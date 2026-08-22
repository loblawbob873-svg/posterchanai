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
  /* DELIBERATELY EMPTY, AND THAT IS THE FEATURE.
   *
   * This was a hardcoded list — Browser, Terminal, Steam — and every entry was wrong in its own way
   * once the launcher learned to read `.desktop` files:
   *
   *   STEAM was offered on every machine, installed or not. A launcher that lists software you do
   *   not have is a launcher you stop believing: "why is steam there? not everyone will install
   *   steam". The scan shows it when it exists and says nothing when it does not.
   *
   *   BROWSER was a second Firefox beside the scanned one, under a different name.
   *
   *   TERMINAL was a duplicate of the client's own Terminal view — and the icon-less one, because
   *   these entries named icons (`globe`, `terminal`, `gamepad`) that are not in the sprite, so all
   *   three rendered with no icon at all. The view has a real one and always did.
   *
   * Anything installed comes from the `.desktop` scan, with the name and icon its own author gave
   * it. Anything of PosterChan's own is a VIEW, and views are already on the desktop, in the start
   * menu and in the sidebar. There is no third category, which is why this list is empty rather
   * than shorter — and it stays a list so a genuine exception has somewhere to go, with `view:` for
   * one of ours and `candidates:` for a program with no `.desktop` entry. */
  const APPS = [];

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
          /* `grid`, and it has to be a symbol that EXISTS. This was `window`, which is not in
           * the sprite — and `iconSvg` emits a `<use href="#i-window">` that draws empty space
           * with no error, no fallback and nothing in the console. So every program found by the
           * .desktop scan appeared in the start menu with no icon at all: "browser terminal have
           * no desktop icons or start menu icons". */
          /* `iconUri` is the app's REAL picture, resolved by the main process (see pc:apps:list);
           * `icon: 'grid'` stays as the fallback for anything whose theme icon could not be found,
           * and it has to be a symbol that EXISTS. This was `window`, which is not in the sprite —
           * and `iconSvg` emits a <use href="#i-window"> that draws empty space with no error, no
           * fallback and nothing in the console. */
          iconUri: String(a.iconUri || ''),
          id: 'app:' + a.id, name: a.name, match: a.match, icon: 'grid',
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
    const net = !s.net ? { text: 'network unknown', known: false, online: false, kind: '' }
              : !s.net.online ? { text: 'offline', known: true, online: false, kind: s.net.kind || '' }
              : { text: s.net.name || s.net.kind || 'online', known: true, online: true,
                  kind: s.net.kind || '',
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
    /* The ACTIVE power profile, for the Power tile's second line. `profileMenu` is the one place
     * that knows a bridge may answer with an object or an array, so it is asked here too rather
     * than re-derived — that difference already cost a laptop its power modes once. */
    const prof = profileMenu(s.power || null);
    return { net, battery: bat, volume: vol, brightness: bright, tor,
             profile: prof.active, profiles: prof.list,
             keepAwake: !!(s.power && s.power.keepAwake),
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

  /** Durable machine claim, independent of the renderer profile and its startup timing. */
  async function provisioned(){
    const os = OS(); if(!os || typeof os.provisioned !== 'function') return false;
    try{ return !!(await os.provisioned()); }catch(_){ return false; }
  }

  async function identity(){
    const os = OS(); if(!os || typeof os.identity !== 'function') return '';
    try{ return String((await os.identity()) || ''); }catch(_){ return ''; }
  }

  /** Move the whole graphical login, not merely the key inside the shared browser profile. */
  async function activateAccount(npub, sess, meta){
    const os = OS(); if(!os || typeof os.switch !== 'function') return { ok:false, why:'not PosterChanOS' };
    const here = await identity();
    if(here === String(npub || '')) return { ok:true, current:true };
    try{ return await os.switch(String(npub || ''), { sess: sess || null, meta: meta || {} }); }
    catch(e){ return { ok:false, why:String((e && e.message) || e) }; }
  }

  async function logoutSession(){
    const os = OS(); if(!os || typeof os.logout !== 'function') return { ok:false, why:'not PosterChanOS' };
    try{ return await os.logout(); }catch(e){ return { ok:false, why:String((e && e.message) || e) }; }
  }

  /* ── THE VISIBLE HALF ─────────────────────────────────────────────────────────────────────────
   *
   * Deliberately small. Everything above decides; this draws. It is also the only part that cannot
   * be tested here, so it is kept to the point where reading it is enough — anything that needed
   * thinking about was moved up.
   */
  const H = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  const ICO = (n, cls) => `<svg class="ic${cls ? ' ' + cls : ''}" aria-hidden="true"><use href="#i-${n}"></use></svg>`;

  /* WHICH WIFI GLYPH. Signal is drawn in three steps, the way every phone and every Windows tray
   * draws it, because a single bar-less "connected" icon cannot tell somebody their wifi is the
   * reason a page will not load. An UNKNOWN reading is not full strength — it takes the plain
   * glyph, which is what "on, and I did not measure how well" looks like. */
  function wifiIcon(net){
    const n = net || {};
    if(!n.known) return 'wifi-off';
    if(!n.online) return 'wifi-off';
    if(n.kind && n.kind !== 'wifi') return 'ethernet';
    if(n.signal == null) return 'wifi';
    if(n.signal < 34) return 'wifi-low';
    if(n.signal < 67) return 'wifi-mid';
    return 'wifi';
  }

  /* WHICH SPEAKER GLYPH — and mute is a DIFFERENT icon, not a dimmed one. A muted machine that
   * looks like a quiet one is how somebody spends a minute turning a slider up on silence. */
  const volIcon = (v) => (!v || !v.known) ? 'volume-mute'
                       : (v.muted || v.percent === 0) ? 'volume-mute' : 'volume';

  /* THE BATTERY IS DRAWN, NOT PICKED — flat, with the charge as a rectangle sized from the reading.
   *
   * Asked for as "battery/charging should be a flat icon": it was the number plus a ⚡ EMOJI, which
   * takes the emoji font's own colour and shape and sits at a different weight to every other glyph
   * in the tray. A sprite symbol cannot carry the level (a `<use>` takes no parameters), and a set
   * of ten symbols would be ten chances for the level and the picture to disagree, so the shell is
   * the sprite's and the fill is computed here. Inline, because that is the only way one SVG can
   * hold both. */
  function batterySvg(pct, charging){
    const p = Math.max(0, Math.min(100, Number(pct) || 0));
    const w = (15 * p) / 100;
    /* Below about a pixel the fill is invisible anyway, and a 0.2-wide rounded rect renders as a
     * smudge — an empty battery is drawn empty. */
    const fill = w >= 1 ? `<rect class="os-bat-fill" x="4.2" y="9.4" width="${w.toFixed(1)}"
        height="5.2" rx="1" fill="currentColor" stroke="none"/>` : '';
    const bolt = charging ? `<path d="M12.8 8.2l-3.2 4h2.6l-1 3.6 3.4-4.2h-2.6z"
        fill="currentColor" stroke="none" class="os-bat-bolt"/>` : '';
    return `<svg class="ic os-bat${p <= 15 && !charging ? ' os-bat-low' : ''}" viewBox="0 0 24 24"
        aria-hidden="true"><rect x="2.4" y="7.6" width="17" height="8.8" rx="2.2"/>
      <path d="M21.6 10.6v2.8" stroke-width="2.6" stroke-linecap="round"/>${fill}${bolt}</svg>`;
  }

  /* ONE BUTTON IN THE BOTTOM-RIGHT CORNER, AND THAT IS THE WHOLE POINT.
   *
   * This was a strip of text chips: `95% Tribble` `72%` `33%` `🧅 off` `100% ⚡` `⏻`. Six controls,
   * five of them a bare percentage with no picture of what the percentage was OF, and the only way
   * to tell the volume from the brightness was that one of them was bigger. Reported as "volume and
   * brightness have no icons", "battery/charging should be a flat icon", "network icon with no way
   * to configure networking", and then, plainly: "we need the desktop experience to mirror win11
   * basically — including all the control in the bottom-right".
   *
   * Windows groups exactly these three — network, sound, battery — into ONE button that opens Quick
   * Settings, and everything else is inside it. That is what this is. The individual popovers are
   * all still here and still reachable; they are reached from the flyout now instead of from six
   * buttons competing for the corner of a taskbar.
   *
   * A reading that could not be TAKEN still shows: the wifi glyph goes to `wifi-off` and the button
   * says so in its title. An icon quietly omitted is indistinguishable from hardware that is fine. */
  function panelHTML(sum){
    const s = sum || {};
    const bits = [];
    const title = [];
    bits.push(ICO(wifiIcon(s.net), 'os-tr-net'));
    title.push(s.net && s.net.known ? String(s.net.text || '') : 'network unknown');
    if(s.volume && s.volume.known){
      bits.push(ICO(volIcon(s.volume), 'os-tr-vol'));
      title.push(s.volume.muted ? 'muted' : s.volume.percent + '% volume');
    }
    if(s.battery && s.battery.known && s.battery.present){
      bits.push(batterySvg(s.battery.percent, s.battery.charging));
      title.push(s.battery.percent + '%' + (s.battery.charging ? ' charging' : ''));
    }
    /* Tor gets its own place in the group ONLY when it is on, the way Windows shows a VPN. Off, it
     * is a tile inside the flyout — a corner of a taskbar is not where a switch that is not
     * switched belongs. */
    if(s.tor && s.tor.present && s.tor.on){
      bits.push(ICO('shield', 'os-tr-tor'));
      title.push(s.tor.bootstrapped >= 100 ? 'Tor on' : 'Tor ' + s.tor.bootstrapped + '%');
    }
    return `<div class="os-panel"><button class="os-tray-group" data-os="quick"
        title="${H(title.join(' · '))}" aria-label="${H('Quick settings — ' + title.join(', '))}"
        aria-haspopup="dialog">${bits.join('')}</button></div>`;
  }

  /* ── QUICK SETTINGS ────────────────────────────────────────────────────────────────────────────
   *
   * The flyout the group opens: the four things people change without opening anything (wifi, Tor,
   * Keep Awake, power), then the two sliders, then the battery. Laid out like Windows 11's
   * because that is what was asked for, and because a tile grid over sliders is genuinely the right
   * shape — a tile is a state you toggle, a slider is a value you drag, and mixing them into one
   * list makes both worse.
   *
   * The tiles carry `data-os`, so `bindPanel` wires them exactly as it wired the old chips: the
   * network list, the Tor switch and the power menu are the SAME popovers, not second copies. */
  function quickHTML(sum, opts){
    const s = sum || {};
    const o = opts || {};
    const tiles = [];
    tiles.push(`<button class="os-qs-tile${s.net && s.net.online ? ' on' : ''}" data-os="net">
        ${ICO(wifiIcon(s.net))}<b>Wi-Fi</b><span>${H(s.net && s.net.known
          ? (s.net.text || '') : 'could not be read')}</span></button>`);
    if(s.tor && s.tor.present)
      tiles.push(`<button class="os-qs-tile${s.tor.on ? ' on' : ''}" data-os="tor">
          ${ICO('shield')}<b>Tor</b><span>${s.tor.on
            ? (s.tor.bootstrapped >= 100 ? 'On' : H(s.tor.bootstrapped) + '%') : 'Off'}</span></button>`);
    tiles.push(`<button class="os-qs-tile${s.keepAwake ? ' on' : ''}" data-os="awake">
        ${ICO('eye')}<b>Keep Awake</b><span>${s.keepAwake ? 'On' : 'Off'}</span></button>`);
    tiles.push(`<button class="os-qs-tile" data-os="power">
        ${ICO('power')}<b>Power</b><span>${H(s.profile || 'Sleep, restart…')}</span></button>`);

    const rows = [];
    if(s.volume && s.volume.known)
      rows.push(`<div class="os-qs-row">
        <button class="os-qs-ic" data-os="mute" title="${s.volume.muted ? 'Unmute' : 'Mute'}"
          aria-label="${s.volume.muted ? 'Unmute' : 'Mute'}">${ICO(volIcon(s.volume))}</button>
        <input class="os-qs-range os-boostable" data-qs="vol" type="range" min="0" max="${VOL_MAX}"
               value="${H(s.volume.percent)}" aria-label="Volume">
        <span class="os-qs-val" data-val="vol">${H(s.volume.percent)}%</span>
        <button class="os-qs-more" data-os="outputs" title="Change output device"
          aria-label="Change output device">${ICO('chevron-right')}</button></div>`);
    if(s.brightness && s.brightness.known)
      rows.push(`<div class="os-qs-row">
        <span class="os-qs-ic os-qs-static">${ICO('sun')}</span>
        <input class="os-qs-range" data-qs="bright" type="range" min="1" max="100"
               value="${H(s.brightness.percent)}" aria-label="Brightness">
        <span class="os-qs-val" data-val="bright">${H(s.brightness.percent)}%</span></div>`);

    const foot = [];
    if(s.battery && s.battery.known && s.battery.present)
      foot.push(`<span class="os-qs-bat">${batterySvg(s.battery.percent, s.battery.charging)}`
        + `${H(s.battery.percent)}%${s.battery.charging ? ' charging' : ''}</span>`);
    if(s.volume && s.volume.known)
      foot.push(`<button class="os-qs-link" data-os="mixer">${ICO('sliders')}Volume mixer</button>`);

    return `<div class="os-qs">
      <div class="os-qs-tiles">${tiles.join('')}</div>
      ${rows.join('')}
      ${foot.length ? `<div class="os-qs-foot">${foot.join('')}</div>` : ''}</div>`;
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

  /* ONE-SHOT: the next openPop renders INSIDE the flyout instead of beside it.
   *
   * Set by the flyout's own tiles. Doing it here rather than in each of netPop/torPop/powerPop is
   * what makes their ERROR branches navigate too -- "Tor could not be read on this machine" opens
   * its own openPop, and a refusal that appears in the top-left corner is the same bug wearing a
   * different message. Consumed by the first openPop it reaches, so it cannot leak into whatever
   * the person presses next. */
  let _asSub = false;
  let _popAnchor = null, _popOpts = null;

  /** The same markup with a way back out of it. */
  function withBack(html){
    const back = `<button class="os-pop-back" data-os="quickback" aria-label="Back">`
      + `${ICO('chevron-left')}</button>`;
    if(html.indexOf('<div class="os-pop-h">') >= 0)
      return html.replace('<div class="os-pop-h">', '<div class="os-pop-h">' + back);
    // No header of its own: give it one, or a sub-panel is a dead end.
    return `<div class="os-pop-h">${back}</div>` + html;
  }

  function positionPop(d, anchor, opts){
    if(!d || !anchor) return;
    const o = opts || {};
    try{
      const r = anchor.getBoundingClientRect();
      const w = d.offsetWidth || 280, h = d.offsetHeight || 200;
      const zf = (anchor.offsetWidth > 0 && r.width > 0) ? (r.width / anchor.offsetWidth) : 1;
      const L = r.left / zf, T = r.top / zf, W = r.width / zf;
      const vw = window.innerWidth / zf, vh = window.innerHeight / zf;
      const want = o.align === 'end' ? (L + W) - w + 6 : L + W / 2 - w / 2;
      const x = Math.max(8, Math.min(vw - w - 8, want));
      const y = Math.max(8, Math.min(vh - h - 8, T - h - 10));
      d.style.left = Math.round(x) + 'px';
      d.style.top = Math.round(y) + 'px';
      /* If the panel is taller than the space above the taskbar, scroll inside it. Never let a
       * shutdown control disappear behind the bar merely because a profile row was added. */
      d.style.maxHeight = Math.max(160, Math.floor(T - 16)) + 'px';
      d.style.overflowY = 'auto';
    }catch(_){}
  }

  function openPop(anchor, html, opts){
    if(_asSub && _pop){
      _asSub = false;
      const sub = _pop;
      sub.innerHTML = withBack(html);
      bindPanel(sub);
      requestAnimationFrame(() => positionPop(sub, _popAnchor, _popOpts));
      return sub;
    }
    _asSub = false;
    closePop();
    const o = opts || {};
    const d = document.createElement('div');
    d.className = 'os-pop' + (o.cls ? ' ' + o.cls : '');
    d.innerHTML = html;
    document.body.appendChild(d);
    _popAnchor = anchor; _popOpts = o;
    /* Anchored to the chip and kept ON SCREEN: the tray is at the right-hand end of the taskbar, so
     * a popover laid out from the chip's left edge hangs off the display. */
    try{
      const r = anchor.getBoundingClientRect();
      /* TWO DIFFERENT PIXELS, AND MIXING THEM PUT THIS PANEL IN THE MIDDLE OF THE SCREEN.
       *
       * The client scales the whole page with `body{zoom}` by viewport width (.67–.77 on a desktop
       * tier). Under zoom, `getBoundingClientRect()` answers in VISUAL pixels — what is on the
       * glass — while `offsetWidth` and anything written to `style.left` are LAYOUT pixels. So a
       * rect at x=1708 and a width of 340 are not measured in the same unit, and subtracting one
       * from the other is arithmetic on two scales: the flyout was placed at 1440 layout px, which
       * painted at 1440 × 0.77 = 1109 — a corner panel floating in open desktop, measured on the
       * machine.
       *
       * The factor is taken from the anchor itself rather than read out of a stylesheet or a
       * variable, because it is exactly the ratio between the two things being mixed, whatever set
       * it. Everything below is then in layout px, which is what `style.left` will be read as. */
      void r; // positioning helper measures after the panel has its final content
      positionPop(d, anchor, o);
    }catch(_){}
    /* Closed by a press ANYWHERE else, captured — a click inside a window would otherwise leave it
     * open behind whatever the person went on to do. */
    _popOff = (e) => { if(_pop && !_pop.contains(e.target)) closePop(); };
    setTimeout(() => document.addEventListener('pointerdown', _popOff, true), 0);
    _pop = d;
    return d;
  }

  /* THE CLIENT'S OWN API IS __PC, NOT PC, AND THAT ONE PREFIX KILLED FOUR CONTROLS.
   *
   * app.js publishes window.__PC; there is no window.PC on the page at all. Every call in this file
   * went through the short name, so — measured in the running shell —
   *
   *   • toast() was guarded with `&&`, which was FALSE, so it silently did nothing. Every refusal
   *     this file is careful to word ("could not join", "the mixer could not be read", a power
   *     profile that failed) was written to nowhere.
   *   • the wifi join's uiPrompt THREW, into a catch that reads a throw as "the person cancelled"
   *     — so a secured network could not be joined, ever. That is the whole of "network icon with
   *     no way to configure networking".
   *   • the power menu's uiConfirm threw into a catch that reads a throw as "they said no", so
   *     Restart and Shut down were dead buttons that reported nothing.
   *
   * None of it appeared in any log, and the unit tests passed the entire time, because the harness
   * defines globalThis.PC — the name this file was written against rather than the one the page
   * actually has. So the accessor takes EITHER, the tests now assert against __PC as well, and
   * test_os_shell.py fails if the short name comes back. */
  const APP = () => root.__PC || root.PC || {};
  const toast = (m) => { try{ const a = APP(); if(a.toast) a.toast(m); }catch(_){} };

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
    /* "Looking…" is a tiny panel; the network list is not. Re-anchor after the async answer changes
     * its dimensions or the browser keeps coordinates computed for the loading card, which is how
     * the finished Wi-Fi panel jumps toward the top-left on a scaled desktop. */
    requestAnimationFrame(() => { if(_pop===d) positionPop(d,_popAnchor,_popOpts); });
    body.querySelectorAll('[data-ssid]').forEach(b => b.onclick = async () => {
      const ssid = b.dataset.ssid;
      let pw = '';
      if(b.dataset.sec && ssid !== here){
        closePop();
        const app = APP();
        if(!app.uiPrompt){
          /* NEVER window.prompt: it does not exist in a WebView and it wedges Electron. With no
           * prompt to ask through, say so — a join that silently does nothing is what this was. */
          toast('cannot ask for a password on this build');
          return;
        }
        try{ pw = await app.uiPrompt('Password for ' + ssid, { password: true, ok: 'Join' }); }
        catch(_){ pw = null; }
        if(pw === null) return;
      }
      toast('joining ' + ssid + '…');
      /* A RETURN IS A SUCCESS AND A THROW IS THE FAILURE — net.connect answers {ssid, reused} and
       * rejects with what nmcli said. This read `r.ok`, which the bridge has never set, so EVERY
       * successful join reported "could not join" and then the machine connected a second later.
       * Reported as "says could not join then connects": one wrong word about a working feature,
       * which is worse than a broken one, because it teaches people not to trust the screen. */
      try{
        const r = await net.connect(ssid, pw);
        toast((r && r.reused ? 'reconnected to ' : 'joined ') + ssid);
      }catch(e){ toast(String((e && e.message) || e) || ('could not join ' + ssid)); }
      closePop();
      refresh();
    });
  }

  /* VOLUME GOES PAST 100, BRIGHTNESS DOES NOT, and they are not the same kind of number.
   *
   * 100% is the loudest the hardware is being ASKED for, not the loudest it can be — everything
   * above it is PipeWire scaling the samples up in software, which is what every desktop mixer
   * offers and what a quiet recording, a laptop speaker or a film mixed for a cinema actually needs.
   * The backend has allowed it all along (audio.js MAX = 1.5); only these sliders stopped at 100, so
   * the ceiling was three copies of a number in the markup rather than a decision anybody made.
   *
   * 150 matches audio.js so the slider cannot ask for something `clamp` will silently reduce — a
   * control that lands somewhere other than where it was dropped is worse than one that stops.
   * Brightness has no equivalent: 100% is the panel at full power and there is nothing above it, so
   * it keeps the default. */
  var VOL_MAX = 150;

  /* A SLIDER, and the reading it started from. Applied as it moves — a volume control you have to
   * confirm is one you cannot use to turn something down quickly. */
  function sliderPop(anchor, title, value, extra, onSet, max){
    const top = max || 100;
    const d = openPop(anchor, `<div class="os-pop-h">${H(title)}</div>
      <div class="os-pop-b"><input class="os-pop-range${top > 100 ? ' os-boostable' : ''}"
           type="range" min="0" max="${top}"
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
      (n) => a.setVolume(n, 'sink'), VOL_MAX)
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
    /* THE SAME PANEL AS THE VOLUME ONE, and that is the whole of the fix.
     *
     * "power button menu is garbage! it's a menu that opens up top-left. make it nice, centered,
     * big!" — then "maybe power button just needs to be improved to function like the volume
     * section", "consistent UI experience and clean".
     *
     * It was already an `openPop`, so the machinery was shared; what was not shared were the two
     * arguments that make the quick panel look like a panel. Without `align:'end'` it was laid out
     * from the chip's LEFT edge, which for a chip at the right-hand end of the taskbar walks the
     * flyout across the screen; and without `os-pop-quick` it fell back to the generic 230-320px
     * box while the volume flyout beside it is a fixed 340. Two chips, one taskbar, two different
     * panels — which is exactly what "garbage" was describing.
     *
     * Same width, same alignment, same rows with an icon and a label, so the tray reads as one
     * thing however you got into it. */
    const row = (act, icon, label, cls) =>
      `<button class="os-pop-row${cls ? ' ' + cls : ''}" data-act="${act}">`
      + `${ICO(icon)}<span class="os-pop-nm">${label}</span></button>`;
    const d = openPop(anchor, `<div class="os-pop-h">${ICO('power')}Power</div>
      ${profs.length ? `<div class="os-pop-b os-pop-profs">${profs.map(x =>
        `<button class="os-pop-btn${x === cur ? ' on' : ''}" data-prof="${H(x)}">${H(x)}</button>`).join('')}</div>` : ''}
      <div class="os-pop-b os-pop-acts os-pop-power-acts">
        ${row('suspend', 'clock', 'Sleep')}
        ${canHib ? row('hibernate', 'battery', 'Hibernate') : ''}
        ${row('reboot', 'refresh', 'Restart')}
        ${row('poweroff', 'power', 'Shut down', 'os-pop-danger')}
      </div>`, { align: 'end', cls: 'os-pop-quick os-pop-power' });
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
        const app = APP();
        try{
          /* NO CONFIRM MEANS NO SHUTDOWN, and it says which. Reading a missing dialog as "they said
           * no" is what made these two buttons do nothing at all. */
          if(!app.uiConfirm){ toast('cannot confirm on this build'); return; }
          ok = await app.uiConfirm(act === 'reboot' ? 'Restart this computer?'
                                                    : 'Shut down this computer?',
                                   { ok: act === 'reboot' ? 'Restart' : 'Shut down', danger: true });
        }catch(_){ ok = false; }
        if(!ok) return;
      }
      closePop();
      try{ await p[act](); }catch(e){ toast(String((e && e.message) || e)); }
    });
  }

  /* ── THE FLYOUT ───────────────────────────────────────────────────────────────────────────────
   *
   * One popover holding the whole of Quick Settings, and every control in it does its work in
   * place: the sliders apply as they move, mute redraws its own icon, and the sub-panels (output
   * devices, the mixer, the screenshot modes) REPLACE the body with a back arrow rather than
   * opening a second popover on top of the first. That is what Windows does and it is the only
   * shape that survives a corner — two stacked flyouts in the bottom-right have nowhere to go.
   */
  let _shotCan = null;               // { ok, region, why } — asked once; it is a package either way

  async function shotAvailable(){
    if(_shotCan) return _shotCan;
    const sh = root.pcShot;
    if(!sh || typeof sh.available !== 'function') return (_shotCan = { ok: false, region: false, why: '' });
    try{ _shotCan = await sh.available(); }
    catch(_){ _shotCan = { ok: false, region: false, why: '' }; }
    return _shotCan;
  }

  async function quickPop(anchor){
    const can = await shotAvailable();
    const d = openPop(anchor, quickHTML(_sum, { shot: can }), { align: 'end', cls: 'os-pop-quick' });
    wireQuick(d);
    return d;
  }

  /* Re-drawn IN PLACE after anything that changes a reading, so the panel never shows a stale
   * number beside a control somebody just moved. The popover is not reopened — reopening drops it
   * out from under the cursor and loses whichever sub-panel somebody is standing in. */
  async function repaintQuick(){
    const d = _pop;
    if(!d || !d.className || String(d.className).indexOf('os-pop-quick') < 0) return;
    await refresh();
    if(_pop !== d) return;
    d.innerHTML = quickHTML(_sum, { shot: _shotCan || { ok: false, region: false } });
    wireQuick(d);
  }

  function wireQuick(d){
    if(!d) return;
    bindPanel(d);
    /* THE SLIDERS APPLY AS THEY MOVE, and they are serialised: `wpctl` and a sysfs write each take
     * milliseconds, and one drag fires a hundred input events. Without the queue those pile up and
     * the level lands wherever the last one to FINISH left it, which is not where the thumb is. */
    d.querySelectorAll('[data-qs]').forEach(r => {
      const kind = r.dataset.qs;
      const val = d.querySelector('[data-val="' + kind + '"]');
      let busy = false, want = null;
      const push = async () => {
        if(busy) return;
        busy = true;
        while(want !== null){
          const n = want; want = null;
          try{
            if(kind === 'vol'){ const a = AUDIO(); if(a) await a.setVolume(n, 'sink'); }
            else { const p = POWER(); if(p) await p.setBrightness(n); }
          }catch(e){
            /* SAID OUT LOUD, ONCE PER DRAG. A brightness slider on a machine whose backlight is
             * root-owned moves perfectly and changes nothing; a control that fails in silence is
             * the recurring failure this whole screen exists to stop. */
            if(!r.dataset.said){ r.dataset.said = '1'; toast(String((e && e.message) || e)); }
          }
        }
        busy = false;
      };
      r.oninput = () => { if(val) val.textContent = r.value + '%'; want = Number(r.value); push(); };
    });
  }

  /* THE OUTPUT DEVICE PICKER — "volume should switch audio devices". Replaces the flyout's body,
   * with a way back, and marks the one sound is actually coming out of. */
  async function outputsPanel(){
    const a = AUDIO(); if(!a) return;
    const d = _pop; if(!d) return;
    d.innerHTML = `<div class="os-pop-h"><button class="os-pop-back" data-os="quickback"
        aria-label="Back">${ICO('chevron-left')}</button>Output device</div>
      <div class="os-pop-b">Looking…</div>`;
    bindPanel(d);
    let st = null;
    try{ st = await a.status(); }catch(_){ st = null; }
    if(_pop !== d) return;
    const body = d.querySelector('.os-pop-b');
    if(!body) return;
    if(!st){
      /* "Could not ask" is not "this machine has no speakers", the same distinction the wifi list
       * makes. One is a broken sound server; the other is a server with nothing plugged in. */
      body.innerHTML = `<div class="os-pop-none">The sound devices could not be read on this machine.</div>`;
      return;
    }
    const sinks = st.sinks || [];
    body.innerHTML = sinks.length
      ? sinks.map(x => `<button class="os-pop-row${x.isDefault ? ' on' : ''}" data-sink="${H(x.id)}">
           <span class="os-pop-nm">${H(x.name)}</span>
           ${x.isDefault ? '<span class="os-pop-sig">in use</span>' : ''}</button>`).join('')
      : `<div class="os-pop-none">This machine has no sound output.</div>`;
    body.querySelectorAll('[data-sink]').forEach(b => b.onclick = async () => {
      try{ await a.setDefault(Number(b.dataset.sink)); }
      catch(e){ toast(String((e && e.message) || e)); return; }
      await refresh();
      if(_pop === d) outputsPanel();
    });
  }

  /* THE APP MIXER — one row per playing application, which is the "proper app-level mixer".
   *
   * NOTHING PLAYING IS A REAL ANSWER and it is written as one. An empty mixer drawn as a blank
   * panel is indistinguishable from a mixer that could not read the machine, which is the same
   * mistake as an empty wifi list. */
  async function mixerPanel(){
    const a = AUDIO(); if(!a || typeof a.mixer !== 'function') return;
    const d = _pop; if(!d) return;
    d.innerHTML = `<div class="os-pop-h"><button class="os-pop-back" data-os="quickback"
        aria-label="Back">${ICO('chevron-left')}</button>Volume mixer</div>
      <div class="os-pop-b">Looking…</div>`;
    bindPanel(d);
    let rows = null, st = null;
    try{ [rows, st] = await Promise.all([a.mixer(), a.status()]); }catch(_){ rows = null; st = null; }
    if(_pop !== d) return;
    const body = d.querySelector('.os-pop-b');
    if(!body) return;
    if(rows === null || st === null){
      body.innerHTML = `<div class="os-pop-none">The mixer could not be read on this machine.</div>`;
      return;
    }
    const chooser = (label, kind, list) => `<label class="fld os-mix-device"><span>${label}</span>
      <select class="input" data-device="${kind}" aria-label="${label}">
        ${(list||[]).map(x=>`<option value="${H(x.id)}"${x.isDefault?' selected':''}>${H(x.name)}</option>`).join('')}
      </select></label>`;
    const master = (label, kind, v) => `<div class="os-qs-row os-mix-row os-mix-master">
      <button class="os-qs-ic" data-mastermute="${kind}" title="${v&&v.muted?'Unmute':'Mute'}"
        aria-label="${v&&v.muted?'Unmute':'Mute'} ${label}">${ICO(v&&v.muted?'volume-mute':(kind==='source'?'mic':'volume'))}</button>
      <div class="os-mix-body"><span class="os-mix-nm">${label}</span>
        <input class="os-qs-range os-boostable" data-mastervol="${kind}" type="range" min="0" max="${VOL_MAX}"
          value="${H(v&&v.percent!=null?v.percent:100)}" aria-label="${label} volume"></div>
      <span class="os-qs-val" data-masterval="${kind}">${H(v&&v.percent!=null?v.percent+'%':'—')}</span></div>`;
    body.innerHTML = `<div class="os-mix-section"><div class="os-mix-title os-mix-titlebar"><span>Devices</span>
        ${root.pcBluetooth?`<button class="btn btn-ghost small" data-os="bluetooth">${ICO('headphones')} Bluetooth</button>`:''}</div>
        ${chooser('Output device','sink',st.sinks)}${master('Output volume','sink',st.output)}
        ${chooser('Input device','source',st.sources)}${master('Input volume','source',st.input)}</div>
      <div class="os-mix-section"><div class="os-mix-title">Apps</div>${rows.length ? rows.map(r => `<div class="os-qs-row os-mix-row">
        <button class="os-qs-ic" data-mix="${H(r.id)}" title="${r.muted ? 'Unmute' : 'Mute'}"
          aria-label="${r.muted ? 'Unmute' : 'Mute'} ${H(r.name)}"
          >${ICO(r.muted ? 'volume-mute' : 'volume')}</button>
        <div class="os-mix-body"><span class="os-mix-nm">${H(r.name)}</span>
          <input class="os-qs-range os-boostable" data-mixvol="${H(r.id)}" type="range" min="0" max="${VOL_MAX}"
                 value="${H(r.percent == null ? 100 : r.percent)}" aria-label="${H(r.name)} volume"></div>
        <span class="os-qs-val" data-val="mix${H(r.id)}">${H(r.percent == null ? '—' : r.percent + '%')}</span>
      </div>`).join('') : '<div class="os-pop-none">Nothing is playing.</div>'}</div>`;
    /* The body is replaced AFTER the asynchronous PipeWire reads. `bindPanel(d)` above wired the
     * temporary "Looking…" markup, not these newly-created controls, so Bluetooth had a data-os
     * attribute and no click handler at all. Wire the final subtree before its specialist inputs. */
    bindPanel(body);
    body.querySelectorAll('[data-device]').forEach(sel => sel.onchange = async () => {
      try{ await a.setDefault(Number(sel.value)); }
      catch(e){ toast(String((e&&e.message)||e)); return; }
      if(_pop===d) mixerPanel();
    });
    body.querySelectorAll('[data-mastervol]').forEach(sl => {
      const kind=sl.dataset.mastervol, val=body.querySelector('[data-masterval="'+kind+'"]');
      let busy=false, want=null;
      const push=async()=>{ if(busy)return; busy=true; while(want!==null){ const n=want; want=null;
        try{ await a.setVolume(n,kind); }catch(e){ if(!sl.dataset.said){sl.dataset.said='1';toast(String((e&&e.message)||e));} }
      } busy=false; };
      sl.oninput=()=>{ if(val)val.textContent=sl.value+'%'; want=Number(sl.value); push(); };
    });
    body.querySelectorAll('[data-mastermute]').forEach(b=>b.onclick=async()=>{
      const kind=b.dataset.mastermute, v=kind==='source'?st.input:st.output;
      try{ await a.setMuted(!(v&&v.muted),kind); }catch(e){toast(String((e&&e.message)||e));return;}
      if(_pop===d)mixerPanel();
    });
    body.querySelectorAll('[data-mixvol]').forEach(sl => {
      const id = sl.dataset.mixvol;
      const val = body.querySelector('[data-val="mix' + id + '"]');
      let busy = false, want = null;
      const push = async () => {
        if(busy) return;
        busy = true;
        while(want !== null){
          const n = want; want = null;
          try{ await a.setStreamVolume(Number(id), n); }
          catch(e){ if(!sl.dataset.said){ sl.dataset.said = '1'; toast(String((e && e.message) || e)); } }
        }
        busy = false;
      };
      sl.oninput = () => { if(val) val.textContent = sl.value + '%'; want = Number(sl.value); push(); };
    });
    body.querySelectorAll('[data-mix]').forEach(b => b.onclick = async () => {
      const row = rows.filter(x => String(x.id) === b.dataset.mix)[0];
      try{ await a.setStreamMuted(Number(b.dataset.mix), !(row && row.muted)); }
      catch(e){ toast(String((e && e.message) || e)); return; }
      if(_pop === d) mixerPanel();
    });
  }

  async function bluetoothPanel(scan){
    const bt=root.pcBluetooth,d=_pop;if(!bt||!d)return;
    d.innerHTML=`<div class="os-pop-h"><button class="os-pop-back" data-os="mixer" aria-label="Back">${ICO('chevron-left')}</button>Bluetooth audio</div><div class="os-pop-b"><div class="os-pop-none">${scan?'Scanning…':'Looking…'}</div></div>`;
    bindPanel(d);let st;try{st=await bt.status(!!scan);}catch(e){st={available:false,error:String(e)}}
    if(_pop!==d)return;const body=d.querySelector('.os-pop-b');if(!body)return;
    if(!st.available){body.innerHTML=`<div class="os-pop-none">Bluetooth is unavailable.${st.error?' '+H(st.error):''}</div>`;return;}
    const devices=(st.devices||[]).slice().sort((a,b)=>Number(b.connected)-Number(a.connected)||Number(b.paired)-Number(a.paired)||a.name.localeCompare(b.name));
    body.innerHTML=`<div class="os-bt-tools"><label><input type="checkbox" data-bt-power ${st.powered?'checked':''}> Bluetooth</label><button class="btn btn-ghost small" data-bt-scan ${st.powered?'':'disabled'}>${ICO('refresh')} Scan</button></div>
      <div class="os-bt-list">${!st.powered?'<div class="os-pop-none">Turn Bluetooth on to find devices.</div>':devices.length?devices.map(x=>`<div class="os-bt-row"><span class="os-bt-icon">${ICO(x.audio?'headphones':'monitor')}</span><span class="os-bt-name"><b>${H(x.name||x.address)}</b><small>${x.connected?'Connected':x.paired?'Paired':'Available'}</small></span><span class="os-bt-actions">${x.connected?`<button class="btn btn-ghost small" data-bt-act="disconnect" data-bt-mac="${H(x.address)}">Disconnect</button>`:x.paired?`<button class="btn small" data-bt-act="connect" data-bt-mac="${H(x.address)}">Connect</button>`:`<button class="btn small" data-bt-act="pair" data-bt-mac="${H(x.address)}">Pair</button>`}${x.paired?`<button class="btn btn-ghost small" data-bt-act="remove" data-bt-mac="${H(x.address)}" title="Forget device">Forget</button>`:''}</span></div>`).join(''):'<div class="os-pop-none">No devices found. Put the device in pairing mode and scan again.</div>'}</div>`;
    const power=body.querySelector('[data-bt-power]');if(power)power.onchange=async()=>{power.disabled=true;const r=await bt.power(power.checked);if(!r.ok)toast(r.error||'Bluetooth power failed');bluetoothPanel(power.checked);};
    const scanBtn=body.querySelector('[data-bt-scan]');if(scanBtn)scanBtn.onclick=()=>bluetoothPanel(true);
    body.querySelectorAll('[data-bt-act]').forEach(b=>b.onclick=async()=>{b.disabled=true;const r=await bt.device(b.dataset.btMac,b.dataset.btAct);if(!r.ok)toast(r.error||'Bluetooth action failed');await bluetoothPanel(false);});
  }

  /* A SCREENSHOT, AND THEN A SENTENCE SAYING WHERE IT WENT. A screenshot whose only feedback is a
   * shutter nobody can hear is a key people press three times and then go looking in four folders
   * for — and if the tool is missing they never find out at all.
   *
   * The flyout closes FIRST and the capture waits a frame, or the picture is of the flyout. */
  async function takeShot(mode){
    const sh = root.pcShot;
    if(!sh || typeof sh.take !== 'function'){ toast('screenshots are not available here'); return null; }
    /* CHOOSING AN AREA IS THE DEFAULT NOW (Print picks a rectangle, Shift+Print takes the screen),
     * so this is the path a bare keypress takes — and it must not be the path that does nothing.
     * Region needs `slurp`, which is a separate package; without it the old code would have sent
     * `mode:'region'` to a helper that cannot do it, on the key somebody presses most. Falling back
     * to the whole screen and SAYING so is the only answer that still produces a screenshot. */
    if(mode === 'region'){
      const can = await shotAvailable();
      if(can && can.ok && !can.region){
        toast('Choosing an area needs slurp (gui-apps/slurp) — took the whole screen instead');
        mode = 'screen';
      }
    }
    closePop();
    await new Promise(r => setTimeout(r, 180));
    let res = null;
    try{ res = await sh.take({ mode: mode || 'screen' }); }
    catch(e){ toast(String((e && e.message) || e)); return null; }
    /* CANCELLED IS NOT FAILED. `slurp` exits nonzero when somebody presses Escape, and a toast
     * apologising every time a person changes their mind is noise. */
    if(!res || res.cancelled) return res || null;
    if(!res.ok){ toast(res.why || 'the screenshot did not save'); return res; }
    const m = /Screenshots\/[^/]+$/.exec(String(res.path || ''));
    toast('Screenshot saved · ' + (m ? m[0] : res.path) + (res.copied ? ' · copied' : ''));
    return res;
  }

  /* The screenshot tile OFFERS THE MODES rather than assuming one — but it offers them in the order
   * the keyboard does. Print picks a rectangle and Shift+Print takes the screen, so "Choose an
   * area" leads here too; a menu whose first row is the one the key does NOT do teaches the wrong
   * thing about the key. */
  function shotPanel(){
    const d = _pop; if(!d) return;
    const can = _shotCan || { ok: true, region: false };
    d.innerHTML = `<div class="os-pop-h"><button class="os-pop-back" data-os="quickback"
        aria-label="Back">${ICO('chevron-left')}</button>Screenshot</div>
      <div class="os-pop-b os-pop-acts">
        ${can.region ? `<button class="os-pop-row" data-shot="region">Choose an area…</button>` : ''}
        <button class="os-pop-row" data-shot="screen">Whole screen</button>
      </div>${can.region ? ''
        : `<div class="os-pop-none">Choosing an area needs slurp (gui-apps/slurp), which is not installed.</div>`}`;
    bindPanel(d);
    d.querySelectorAll('[data-shot]').forEach(b => b.onclick = () => takeShot(b.dataset.shot));
  }

  /** Was this control pressed inside the open flyout, rather than being a taskbar chip of its own? */
  const inFlyout = (b) => { try{ return !!(_pop && _pop.contains(b)); }catch(_){ return false; } };

  /** Wires whichever chips are present in `into`. Safe to call on markup that has none. */
  function bindPanel(into){
    if(!into) return;
    into.querySelectorAll('[data-os]').forEach(b => b.onclick = (e) => {
      e.stopPropagation();
      const kind = b.dataset.os;
      /* A second press on the SAME chip closes it. Without this the popover is dismissed by the
       * outside-press handler and reopened by the click, so the button appears not to work. */
      if(_pop && _pop.dataset.kind === kind){ closePop(); return; }
      /* THE FLYOUT'S OWN BUTTONS DO NOT OPEN A SECOND POPOVER — they replace what is in this one,
       * and are therefore handled BEFORE the closePop below. Two stacked popovers in the
       * bottom-right corner have nowhere to go, which is why Windows makes Quick Settings navigate
       * within itself, and why a sub-panel that closed its own host would simply vanish. */
      if(kind === 'quickback'){
        if(_pop){ _pop.innerHTML = quickHTML(_sum, { shot: _shotCan || { ok: false } }); wireQuick(_pop); }
        return;
      }
      /* THE FLYOUT'S OWN TILES NAVIGATE WITHIN IT. Wi-Fi, Tor and Power were falling through to
       * the popover path below, which opens a SECOND popover anchored to a tile that lives inside
       * the first one -- and an anchor inside a corner flyout has nowhere to put a menu, so it
       * landed in the top-left of the screen. Reported as "wifi network picker loads a menu in
       * top-left still! supposed to be like win11". Same rule as outputs/mixer/shot, which were
       * already sub-panels: a tile in Quick Settings replaces the body and offers a way back. */
      if(inFlyout(b) && (kind === 'net' || kind === 'tor' || kind === 'power')){
        _asSub = true;
        const open = kind === 'net' ? netPop : (kind === 'tor' ? torPop : powerPop);
        Promise.resolve(open(b)).catch(() => {}).then(() => { _asSub = false; });
        return;
      }
      if(kind === 'outputs'){ outputsPanel(); return; }
      if(kind === 'mixer'){ mixerPanel(); return; }
      if(kind === 'bluetooth'){ bluetoothPanel(false); return; }
      if(kind === 'shot'){ shotPanel(); return; }
      if(kind === 'awake'){
        const p = POWER();
        if(!p || typeof p.setKeepAwake !== 'function') return;
        Promise.resolve(p.setKeepAwake(!(_sum && _sum.keepAwake)))
          .then(() => repaintQuick()).catch(e => toast(String((e && e.message) || e)));
        return;
      }
      if(kind === 'mute'){
        const a = AUDIO();
        const muted = !!(_sum && _sum.volume && _sum.volume.muted);
        if(a) a.setMuted(!muted, 'sink').then(repaintQuick, (err) => toast(String((err && err.message) || err)));
        return;
      }
      closePop();
      const done = (d) => { if(d) d.dataset.kind = kind; };
      if(kind === 'quick') quickPop(b).then(() => done(_pop));
      else if(kind === 'net') netPop(b).then(() => done(_pop));
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
      try{ const r = await launch(b.dataset.app); if(r && r.why) toast(r.why); }
      catch(e){ toast(String((e && e.message) || e)); }
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
                profileMenu, machineApps, mergedApps, allApps, wifiIcon, volIcon, batterySvg,
                ensureAccount, provisioned, identity, activateAccount, logoutSession,
                panelHTML, quickHTML, taskbarHTML, launcherHTML, render, watch,
                takeShot, shotAvailable, closePop,
                setViewOpener, refresh, paintTray, bindApps, bindPanel,
                summary: () => _sum, rows: () => _rows, readAt: () => _readAt };
  root.PCOSShell = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);
