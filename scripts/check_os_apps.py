#!/usr/bin/env python3
"""PosterChan OS — open every app in a window on a REAL instance and check it actually works.

    venv-unified/bin/python scripts/check_os_apps.py [base_url]

check_os_desktop.py drives os.js against a stub: it proves the window manager is correct, and
proves nothing about the features living inside the windows. This one loads the real client, enters
the desktop, and opens each launcher entry in turn — which is the only way to catch the class of bug
that made a user say "all the apps buttons don't work on the OS".

Assertions, each a way a feature breaks specifically INSIDE a window and nowhere else:

  view-blank           The window opened and painted nothing. A feature reached through the launcher
                       must render the same as it does from the sidebar, because the launcher calls
                       exactly what the sidebar calls (switchView) — a blank one means the view needs
                       something the sidebar click did and the window did not.
  view-threw           Switching into the view raised. Silent in the UI, fatal to the feature.
  window-hoverflow     The window's content scrolls sideways inside its own frame. A view laid out
                       for a full-width column does this the moment it is put in a 700px window.
  escapes-window       A visible element sticks out past its window's edge. Usually a rule sized in
                       viewport units (100dvh / 100vw), which inside a window still measures the
                       SCREEN — the single most likely way a view misbehaves here and nowhere else.
  reply-broken         Reply on a post inside a window does not open a usable dialog. Reply is a
                       MODAL, and modals rendering behind the desktop was the bug that made half
                       the apps look dead — this asserts the path, not the stylesheet.
  signed-in-app-missing / no-account-switcher
                       Launcher entries and the tray avatar that only exist for a signed-in user.
                       Checked by stubbing PC.me(), because the audit runs as a guest and "skipped
                       for a guest" is how a gate that was broken for EVERYBODY stayed hidden.
  folder-broken        The Nostr Games folder does not hold the games, does not open one when
                       clicked, or steals the live #feed from the window that had it.
  no-launcher-refresh  The desktop cannot rebuild its launcher when the identity arrives. Icons are
                       built once at enter(), and a remembered desktop opens before login resolves.
  view-not-claimed     A window rendered something other than a timeline but left VIEW at 'home' or
                       'global'. #feed is shared, and the live timeline appends to it on those two
                       views — so incoming posts overwrite whatever that window was showing.
  exit-dead-view       Leaving the desktop lands the classic UI on a view only the desktop knows
                       (the Music window's own screen), which has no sidebar entry to leave by.
  music-not-an-app     Music does not open as a window. It is a library you browse; the floating
                       player bar is the transport, not the app.
  noti-centre          The clock does not open a working notification centre (off screen, stacked
                       under something, no rows and no empty state, no reply/react, or Escape does
                       not close it).
  feed-astray          After opening a window, the live #feed is not inside it. Everything renders
                       into #feed, so if it is elsewhere the feature painted where nobody looks.

Runs as a guest, so it only reaches the views a guest can reach; the ones needing a key are
reported as skipped rather than passing quietly.

Exit 0 = clean, 1 = problems (printed), 2 = could not run (no Chrome / site unreachable).
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://poster.place"
PORT = int(os.environ.get("PC_CHECK_PORT") or 9488)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-os-apps"

VIEWPORT = (1600, 900)

ENTER = r"""(async () => {
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  if (!window.PCOS) return { err: 'no PCOS' };
  if (!PCOS.isOn()) { PCOS.enter(); await sleep(400); }
  const icons = [...document.querySelectorAll('.os-icon')].map(b => ({
    view: b.dataset.view || '', label: (b.textContent||'').trim() }));
  // Every real view, folded or not. The desktop icons collapse the games into one folder, and
  // probing only what is on the desktop would silently stop opening the six apps inside it.
  const flat = [...document.querySelectorAll('.sidebar .nav .nav-item[data-view]')]
                 .map(b => b.dataset.view).filter(Boolean);

  // The clock is the notification centre, the way Windows does it.
  const clock = document.getElementById('os-clock');
  let noti = { hasClock: !!clock };
  if (clock) {
    clock.click(); await sleep(400);
    const p = document.getElementById('os-noti');
    noti.opened = !!p;
    if (p) {
      const pr = p.getBoundingClientRect();
      noti.onScreen = pr.right <= window.innerWidth + 1 && pr.top >= -1 && pr.width > 200;
      noti.rows = p.querySelectorAll('.notif').length;
      noti.acts = p.querySelectorAll('.os-noti-act .os-na').length;
      noti.empty = !!p.querySelector('.empty');
      // Above the desktop, or it is open and unclickable — the bug modals had.
      const hit = document.elementFromPoint(pr.left + pr.width/2, pr.top + 24);
      noti.reachable = !!(hit && p.contains(hit));
    }
    document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}));
    await sleep(250);
    noti.closes = !document.getElementById('os-noti');
  }
  /* Signed-in surfaces. The audit runs as a GUEST, so everything gated on "who is signed in" was
   * reported as skipped — which is exactly how a bug that hid them from EVERYONE went unnoticed:
   * the client keeps ME inside its IIFE, so the old window.ME gate was undefined for all users, not
   * just guests. The gate is PC.me(), so stubbing that is a faithful test of the launcher's wiring
   * without signing a throwaway key into a production instance. */
  let asUser = { stubbed: false };
  if (!(window.__PC.me && window.__PC.me())) {
    const real = window.__PC.me;
    window.__PC.me = () => ({ pubkey: 'f'.repeat(64), npub: 'npub1testtesttest' });
    try {
      /* Deliberately WITHOUT re-entering: this is the real boot order — the desktop is already open
       * (a remembered osMode opens during boot) and the identity arrives afterwards. PCOS.refresh()
       * is what has to notice. Tearing the desktop down and rebuilding it would test a path that
       * never happens and would have passed while the desktop was missing Music, My Profile, Go Live
       * and the tray avatar for the whole session. */
      if (PCOS.refresh) PCOS.refresh(); else asUser.noRefresh = true;
      await sleep(400);
      asUser.stubbed = true;
      asUser.icons = [...document.querySelectorAll('.os-icon')].map(b => b.dataset.view || '');
      // The account lives at the foot of the START MENU now, the way Windows 11 does it.
      document.getElementById('os-start').click(); await sleep(350);
      const chip = document.getElementById('os-acct');
      asUser.tray = !!chip;
      if (chip) {
        // Measure the chip BEFORE clicking: the bug being guarded REMOVES it, so reading its rect
        // afterwards returns 0,0 and the guard would quietly agree with the bug.
        const cr = chip.getBoundingClientRect();
        chip.click(); await sleep(350);
        const pop = document.querySelector('.acct-pop');
        asUser.switcher = !!pop;
        if (pop) {
          const pr = pop.getBoundingClientRect();
          const hit = document.elementFromPoint(pr.left + pr.width/2, pr.top + 10);
          asUser.switcherReachable = !!(hit && pop.contains(hit));
          asUser.switcherAdd = !!pop.querySelector('[data-act="add"]');
          /* …and it must appear NEAR the chip it hangs off. Closing the start menu before measuring
           * the anchor leaves a 0x0 rect at 0,0, so the flyout lands in the top-left corner of the
           * screen — which looks like a rendering fault, not a menu. */
          asUser.switcherStranded = (pr.top < 60 && pr.left < 60 && cr.top > 200);
          asUser.switcherAt = [Math.round(pr.left), Math.round(pr.top)];
          pop.remove();
        }
      }
      document.querySelectorAll('.os-startmenu').forEach(n => n.remove());
      // Music must open as a WINDOW holding the live feed, not merely start playing.
      const mb = [...document.querySelectorAll('.os-icon')].find(b => b.dataset.view === '__music');
      if (mb) {
        mb.click(); await sleep(1500);
        const w = document.querySelector('.osw.focused');
        asUser.musicWin  = !!(w && /Music/.test(w.querySelector('.osw-title').textContent));
        asUser.musicFeed = !!(document.getElementById('feed') || {}).closest('.osw.focused');
        asUser.musicView = window.__PC.VIEW;
        asUser.musicPlayer = !!(w && w.querySelector('.music-app .ma-ctl'));
        /* Leaving the desktop with the Music window open must land the classic UI on a view it
         * actually HAS. 'music' is the Music window's own screen and the sidebar has no entry for
         * it, so exiting on to it strands the classic client on a dead view showing the leftover
         * player, with no nav item to leave by. */
        PCOS.exit(); await sleep(600);
        asUser.exitView = window.__PC.VIEW;
        asUser.exitKnown = !!document.querySelector('.nav-item[data-view="' + window.__PC.VIEW + '"]');
        asUser.exitLeftPlayer = !!document.querySelector('#feed .music-app');
        PCOS.enter(); await sleep(500);
        if (PCOS.refresh) PCOS.refresh();
        await sleep(200);
        // Closing the Music window must close the PLAYER, not replace it with the floating widget.
        const w2 = document.querySelector('.osw.focused');
        if (w2) { w2.querySelector('.osw-x').click(); await sleep(400); }
        const mp = document.getElementById('music-player');
        // Not just the hidden class: on the desktop the widget must not be RENDERED at all.
        asUser.miniAfterClose = !!(mp && !mp.classList.contains('hidden')
                                      && getComputedStyle(mp).display !== 'none');
        document.querySelectorAll('.osw .osw-x').forEach(b => b.click());
        await sleep(150);
      }
    } finally {
      window.__PC.me = real;
      if (PCOS.refresh) PCOS.refresh();
      await sleep(300);
    }
  }
  /* The Nostr Games folder. Three things it must not get wrong: it holds the games, opening one
   * from inside it gives that game a real window, and the folder itself must NOT take the live
   * #feed — a folder owns its own contents, and stealing the feed blanks whatever window was
   * actually showing something. */
  let folder = null;
  // Office is also a folder. Audit Games by identity instead of whichever folder rendered first.
  const fbtn = [...document.querySelectorAll('.os-icon')].find(b => b.dataset.view === 'folder:games');
  if (fbtn) {
    const feedHome = (document.getElementById('feed')||{}).parentElement;
    fbtn.click(); await sleep(450);
    const win = document.querySelector('.osw.focused');
    const tiles = win ? [...win.querySelectorAll('.os-folder .os-icon')].map(b => b.dataset.view) : [];
    folder = { label: (fbtn.textContent||'').trim(), tiles,
               keptFeed: (document.getElementById('feed')||{}).parentElement === feedHome };
    if (tiles.length) {
      const before = document.querySelectorAll('.osw').length;
      win.querySelector('.os-folder .os-icon').click(); await sleep(1200);
      folder.opensGame = document.querySelectorAll('.osw').length > before;
      folder.gameHasFeed = !!(document.getElementById('feed') || {}).closest('.osw.focused');
    }
    document.querySelectorAll('.osw .osw-x').forEach(b => b.click());
    await sleep(200);
  }
  return { on: PCOS.isOn(), icons, flat, noti, asUser, folder };
})()"""

# Open one app, let it settle, then measure the window it landed in.
PROBE = r"""(async (view) => {
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  const out = { view };
  // A folded app (a game) has no desktop icon of its own — open it the way the launcher would.
  const btn = [...document.querySelectorAll('.os-icon')].find(b => b.dataset.view === view);
  window.__osErr = null;
  try { if (btn) btn.click(); else PCOS.routeView(view) || window.__PC.switchView(view); }
  catch (e) { out.threw = String(e && e.message || e); }
  await sleep(1400);

  const win = document.querySelector('.osw.focused');
  if (!win) return Object.assign(out, { noWindow: true });
  const body = win.querySelector('.osw-body');
  const feed = document.getElementById('feed');
  out.feedInside = !!(feed && body.contains(feed));

  // Painted anything? Text OR any element that draws (an empty-state message counts; a spinner
  // that never resolves does not, so spinners are excluded).
  const txt = (body.innerText || '').replace(/\s+/g, ' ').trim();
  out.text = txt.slice(0, 60);
  out.painted = txt.length > 0 || !!body.querySelector('img,canvas,video,input,textarea,button:not(.osw-b)');
  out.spinning = !txt && !!body.querySelector('.spinner');

  const br = body.getBoundingClientRect();
  out.hoverflow = Math.max(0, body.scrollWidth - body.clientWidth);

  // Anything sticking out of the window. Measured, not inferred from the stylesheet: only VISIBLE
  // elements count, and only the ones actually outside the frame by more than a rounding error.
  let worst = null;
  for (const el of body.querySelectorAll('*')) {
    if (el.closest('.osw-bar')) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    // Skip anything held by a scrolling ancestor. A media carousel's slides genuinely sit past the
    // right edge of their track and are reached by scrolling — that is the design, not an escape.
    let clipped = false;
    for (let a = el.parentElement; a && a !== body; a = a.parentElement) {
      const ao = getComputedStyle(a);
      if (/(auto|scroll|hidden|clip)/.test(ao.overflowX + ao.overflowY)) { clipped = true; break; }
    }
    if (clipped) continue;
    const over = Math.max(r.right - br.right, br.left - r.left);
    if (over > 4 && (!worst || over > worst.over)) {
      worst = { over: Math.round(over), tag: el.tagName.toLowerCase(),
                cls: String(el.className || '').slice(0, 44) };
    }
  }
  out.escape = worst;
  /* Which screen the client thinks it is on. #feed is ONE element shared by everything, and the live
   * timeline appends to it whenever VIEW is 'home' or 'global' — so a window rendering anything else
   * must claim VIEW, or incoming social posts write straight over it. That shipped: the Music player
   * kept VIEW='global' and new posts took the window over. */
  out.view = window.__PC.VIEW;
  out.err = window.__osErr;
  return out;
})"""

# Reply is a MODAL, and modals were the thing that broke on this desktop. Assert the path end to
# end from inside a window rather than trusting that the z-index fix covers every caller.
REPLY = r"""(async () => {
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  const btn = [...document.querySelectorAll('.os-icon')].find(b => b.dataset.view === 'global');
  if (!btn) return { skip: 'no Social app' };
  btn.click(); await sleep(1800);
  const win = document.querySelector('.osw.focused');
  const rb = win && win.querySelector('.act[data-a="reply"]');
  if (!rb) return { skip: 'no post with a reply button in the timeline' };
  rb.click(); await sleep(700);
  const bg = document.querySelector('.modal-bg');
  if (!bg) return { opened: false };
  const box = bg.querySelector('.modal') || bg;
  const r = box.getBoundingClientRect();
  const hit = document.elementFromPoint(r.left + r.width/2, r.top + 12);
  const out = { opened: true, reachable: !!(hit && (bg.contains(hit) || hit === bg)),
                coveredBy: '', onScreen: r.top >= -1 && r.bottom <= window.innerHeight + 1 };
  if (!out.reachable && hit) out.coveredBy = (hit.id || hit.className || hit.tagName).toString().slice(0,40);
  bg.remove(); document.body.classList.remove('modal-open');
  return out;
})()"""


async def main():
    chrome = (shutil.which("google-chrome") or shutil.which("chromium")
              or shutil.which("chromium-browser") or shutil.which("google-chrome-stable"))
    if not chrome:
        print("SKIP  no Chrome on this host")
        return 2
    try:
        import websockets
    except ImportError:
        print("SKIP  pip install websockets")
        return 2
    try:
        urllib.request.urlopen(BASE + "/client", timeout=12).read(64)
    except Exception as e:
        print(f"SKIP  {BASE} unreachable: {e}")
        return 2

    shutil.rmtree(PROFILE, ignore_errors=True)
    os.makedirs(PROFILE, exist_ok=True)
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    problems, skipped = [], []
    try:
        page = None
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
                page = [t for t in tabs if t["type"] == "page"][0]
                break
            except Exception:
                await asyncio.sleep(0.5)
        if not page:
            print("SKIP  could not start Chrome")
            return 2

        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
            n = [0]

            async def call(method, params=None):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == n[0]:
                        return msg.get("result")

            async def js(expr, awaited=False):
                r = await call("Runtime.evaluate",
                               {"expression": expr, "returnByValue": True, "awaitPromise": awaited})
                if r.get("exceptionDetails"):
                    if os.environ.get("PC_DEBUG"):
                        print("  DEBUG:", json.dumps(r["exceptionDetails"])[:600])
                    return None
                return r["result"].get("value")

            await call("Runtime.enable")
            await call("Page.enable")
            await call("Emulation.setDeviceMetricsOverride",
                       {"width": VIEWPORT[0], "height": VIEWPORT[1], "deviceScaleFactor": 1,
                        "mobile": False})
            await call("Page.navigate", {"url": BASE + "/client"})

            ready = False
            for _ in range(100):
                await asyncio.sleep(0.3)
                if await js("!!window.PCOS && !!document.querySelector('.nav-item[data-view]')"):
                    ready = True
                    break
            if not ready:
                print("SKIP  the client never finished loading")
                return 2
            # Record uncaught errors while views mount — a view can throw and still look alive.
            await js("window.addEventListener('error', e => { window.__osErr = String(e.message); });"
                     "window.addEventListener('unhandledrejection',"
                     " e => { window.__osErr = 'promise: ' + String(e.reason && e.reason.message || e.reason); });")

            g = await js(ENTER, awaited=True)
            if not g or not g.get("on"):
                print(f"SKIP  the desktop did not open: {g}")
                return 2
            nc = g.get("noti") or {}
            if not nc.get("hasClock"):
                problems.append(("shell", "no-clock", "the taskbar has no clock to open notifications from"))
            elif not nc.get("opened"):
                problems.append(("shell", "noti-centre", "clicking the clock opened no notification centre"))
            else:
                if not nc.get("onScreen"):
                    problems.append(("shell", "noti-centre", "the notification centre is off screen"))
                if not nc.get("reachable"):
                    problems.append(("shell", "noti-centre",
                                     "the notification centre is not clickable — something is stacked "
                                     "over it (the bug modals had against .os-root)"))
                if not nc.get("rows") and not nc.get("empty"):
                    problems.append(("shell", "noti-centre",
                                     "the centre rendered neither notification rows nor an empty state"))
                if nc.get("rows") and not nc.get("acts"):
                    problems.append(("shell", "noti-centre",
                                     "notification rows have no reply/react buttons"))
                if not nc.get("closes"):
                    problems.append(("shell", "noti-centre", "Escape does not close the notification centre"))
            au = g.get("asUser") or {}
            if au.get("noRefresh"):
                problems.append(("shell", "no-launcher-refresh",
                                 "PCOS.refresh() does not exist — the desktop builds its icons once "
                                 "at enter(), which on a remembered desktop happens BEFORE login, so "
                                 "the signed-in entries never appear for the whole session"))
            if au.get("stubbed"):
                want = {"__profile": "My Profile", "__music": "Music", "__golive": "Go Live"}
                have = set(au.get("icons") or [])
                missing = [lbl for v, lbl in want.items() if v not in have]
                if missing:
                    problems.append(("shell", "signed-in-app-missing",
                                     "signed in, the launcher is missing: " + ", ".join(missing)))
                if au.get("musicView") in ("home", "global"):
                    problems.append(("shell", "view-not-claimed",
                                     f"the Music window left VIEW={au.get('musicView')!r} — live "
                                     "social posts append to the shared #feed on that view and will "
                                     "take the player over"))
                if au.get("exitKnown") is False or au.get("exitLeftPlayer"):
                    problems.append(("shell", "exit-dead-view",
                                     f"leaving the desktop landed the classic UI on VIEW="
                                     f"{au.get('exitView')!r}, which the sidebar has no entry for"
                                     + (" — and the music player markup is still in the feed"
                                        if au.get("exitLeftPlayer") else "")))
                if au.get("miniAfterClose"):
                    problems.append(("shell", "music-not-an-app",
                                     "closing the Music window left the floating mini player on "
                                     "screen — closing the app must close the player, not swap it "
                                     "for a smaller one"))
                if au.get("musicPlayer") is False:
                    problems.append(("shell", "music-not-an-app",
                                     "the Music window has no transport controls — it is showing the "
                                     "file manager's Music folder, not a player"))
                if au.get("musicWin") is False or au.get("musicFeed") is False:
                    problems.append(("shell", "music-not-an-app",
                                     "Music does not open as a window holding the feed — "
                                     f"window={au.get('musicWin')} feed-inside={au.get('musicFeed')}"))
                if not au.get("tray"):
                    problems.append(("shell", "no-account-switcher",
                                     "signed in, the start menu has no account chip — which is the "
                                     "only way to the account switcher on the desktop"))
                elif au.get("switcherStranded"):
                    problems.append(("shell", "no-account-switcher",
                                     f"the account flyout opened at {au.get('switcherAt')} — in the "
                                     "corner of the screen, not beside the chip. Its anchor was "
                                     "measured after the start menu had already removed it."))
                elif not au.get("switcher") or not au.get("switcherReachable") \
                        or not au.get("switcherAdd"):
                    problems.append(("shell", "no-account-switcher",
                                     f"the account flyout did not open usably — opened="
                                     f"{au.get('switcher')} clickable={au.get('switcherReachable')} "
                                     f"add-entry={au.get('switcherAdd')}"))

            # __golive RUNS rather than opening a window, so it has no window to measure.
            f = g.get("folder")
            if not f:
                skipped.append("Nostr Games folder (no folder icon on this deployment)")
            else:
                want = {"chess", "ttt", "hangman", "connect4", "blackjack", "holdem"}
                got = set(f.get("tiles") or [])
                if "Nostr Games" not in f.get("label", ""):
                    problems.append(("shell", "folder-broken",
                                     f"the folder icon reads {f.get('label')!r}"))
                if not want.issubset(got):
                    problems.append(("shell", "folder-broken",
                                     "the games folder is missing: " + ", ".join(sorted(want - got))))
                if not f.get("keptFeed"):
                    problems.append(("shell", "folder-broken",
                                     "opening the folder MOVED the live #feed into it — a folder owns "
                                     "its own contents, and taking the feed blanks the window that "
                                     "was showing something"))
                if not f.get("opensGame") or not f.get("gameHasFeed"):
                    problems.append(("shell", "folder-broken",
                                     f"a game inside the folder did not open properly — "
                                     f"window={f.get('opensGame')} feed-inside={f.get('gameHasFeed')}"))

            # folder:* opens a folder window, and __* RUNS something; neither has an app view to probe.
            views, seen = [], set()
            for v in (g.get("flat") or [i["view"] for i in g["icons"]]):
                if not v or v.startswith("__") or v.startswith("folder:") or v in seen:
                    continue
                seen.add(v)
                views.append(v)
            print(f"  {len(views)} app(s) in the launcher at {VIEWPORT[0]}x{VIEWPORT[1]}")

            for v in views:
                r = await js(f"({PROBE})({json.dumps(v)})", awaited=True)
                if r is None:
                    problems.append((v, "view-threw", "the probe itself did not evaluate"))
                    continue
                if r.get("missing") or r.get("noWindow"):
                    problems.append((v, "view-blank", "clicking the icon opened no window"))
                    continue
                if r.get("threw") or r.get("err"):
                    problems.append((v, "view-threw", str(r.get("threw") or r.get("err"))[:140]))
                if not r.get("feedInside"):
                    problems.append((v, "feed-astray",
                                     "the live #feed is not inside the window that just opened"))
                if not r.get("painted"):
                    if r.get("spinning"):
                        skipped.append(f"{v} (still loading — guest may not have access)")
                    else:
                        problems.append((v, "view-blank", "the window painted nothing"))
                if v not in ("home", "global") and r.get("view") in ("home", "global"):
                    problems.append((v, "view-not-claimed",
                                     f"the {v} window left VIEW={r.get('view')!r} — the live timeline "
                                     "appends to the shared #feed on those two views, so incoming "
                                     "posts will write over this window's contents"))
                if r.get("hoverflow", 0) > 2:
                    problems.append((v, "window-hoverflow",
                                     f"content scrolls {r['hoverflow']}px sideways inside the window"))
                e = r.get("escape")
                if e:
                    problems.append((v, "escapes-window",
                                     f"<{e['tag']} class={e['cls']!r}> sticks {e['over']}px past the "
                                     "window edge — usually a viewport-unit height/width, which "
                                     "inside a window still measures the whole screen"))
                mark = "ok  " if not any(p[0] == v for p in problems) else "FAIL"
                print(f"    {mark} {v:<14} {r.get('text','')[:44]!r}")

            rp = await js(REPLY, awaited=True)
            if rp is None:
                problems.append(("shell", "reply-broken", "the reply probe did not evaluate"))
            elif rp.get("skip"):
                skipped.append(f"reply ({rp['skip']})")
            elif not rp.get("opened"):
                problems.append(("shell", "reply-broken",
                                 "clicking Reply on a post in a window opened no dialog"))
            elif not rp.get("reachable"):
                problems.append(("shell", "reply-broken",
                                 f"the reply dialog is not clickable — its centre hits "
                                 f"{rp.get('coveredBy')!r}"))
            elif not rp.get("onScreen"):
                problems.append(("shell", "reply-broken", "the reply dialog is partly off screen"))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    if skipped:
        print("\n  skipped (guest cannot reach):")
        for s in skipped:
            print(f"    - {s}")
    if problems:
        print(f"\nFAIL  {len(problems)} problem(s):")
        for view, kind, msg in problems:
            print(f"  [{view}] {kind}: {msg}")
        return 1
    print("\nOK  every app opens and fits in its window")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
