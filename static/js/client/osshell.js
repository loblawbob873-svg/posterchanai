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

  /** Is PosterChan the desktop on this machine? */
  const available = () => !!(WM() && typeof WM().windows === 'function');

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
    { id: 'terminal', name: 'Terminal', match: 'foot', icon: 'terminal', candidates: [
        ['/usr/bin/foot'], ['/usr/bin/footclient'] ] },
    { id: 'steam',    name: 'Steam',    match: 'steam', icon: 'gamepad', candidates: [
        ['/usr/bin/steam'], ['/usr/bin/flatpak', 'run', 'com.valvesoftware.Steam'] ] },
  ];

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

  async function launch(appId){
    const wm = WM(); if(!wm) throw new Error('no compositor here');
    const app = APPS.find(a => a.id === appId);
    if(!app) throw new Error('no such app');
    let open = null;
    try{ open = existingWindow(await wm.windows(), app); }catch(_){}
    if(open){ await wm.focus(open.id); return { focused: open.id }; }
    const r = await wm.launch(app.candidates, { waitMs: 20000, candidates: true });
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
    return { net, battery: bat, volume: vol, brightness: bright,
             canHibernate: !!(s.power && s.power.canHibernate) };
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
    if(s.battery.known && s.battery.present)
      bits.push(`<span class="os-chip" title="Battery">${H(s.battery.percent)}%${s.battery.charging ? ' ⚡' : ''}</span>`);
    bits.push(`<button class="os-chip" data-os="power" title="Power">⏻</button>`);
    return `<div class="os-panel">${bits.join('')}</div>`;
  }

  function taskbarHTML(rows){
    if(!rows || !rows.length) return '';
    return `<div class="os-taskbar">` + rows.map(r =>
      `<button class="os-task${r.focused ? ' on' : ''}" data-win="${H(r.id)}" title="${H(r.title)}">`
      + `${H(r.label)}</button>`).join('') + `</div>`;
  }

  function launcherHTML(){
    return `<div class="os-launcher">` + APPS.filter(a => !a.hidden).map(a =>
      `<button class="os-app" data-app="${H(a.id)}">${H(a.name)}</button>`).join('') + `</div>`;
  }

  /* One draw, from one read. Called on a window event and on a timer — the timer is the slow
   * backstop for the things that have no event at all (a battery draining, a wifi signal moving). */
  async function render(into){
    if(!into || !available()) return;
    let rows = [];
    try{ rows = taskbarRows(await WM().windows()); }catch(_){ rows = []; }
    const sum = panelSummary(await panelState());
    into.innerHTML = launcherHTML() + taskbarHTML(rows) + panelHTML(sum);
    into.querySelectorAll('[data-app]').forEach(b => b.onclick = async () => {
      b.disabled = true;
      try{ const r = await launch(b.dataset.app); if(r && r.why && root.PC) root.PC.toast(r.why); }
      catch(e){ if(root.PC) root.PC.toast(String((e && e.message) || e)); }
      finally{ b.disabled = false; render(into); }
    });
    into.querySelectorAll('[data-win]').forEach(b => b.onclick = async () => {
      try{ await WM().focus(Number(b.dataset.win)); }catch(_){}
      render(into);
    });
  }

  /* Redrawn when the compositor says something changed, not on a fast timer: a taskbar that lags a
   * window by a second is one people stop looking at, and a poll fast enough to hide that is a poll
   * running forever on a laptop battery. */
  async function watch(into){
    if(!available()) return () => {};
    await render(into);
    let off = () => {};
    try{
      await WM().subscribe();
      off = WM().onEvent(() => render(into));
    }catch(_){}
    const t = setInterval(() => render(into), 30000);   // battery and signal have no events
    return () => { try{ off(); }catch(_){} clearInterval(t); };
  }

  const API = { available, APPS, taskbarRows, existingWindow, launch, panelState, panelSummary,
                ensureAccount, panelHTML, taskbarHTML, launcherHTML, render, watch };
  root.PCOSShell = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);
