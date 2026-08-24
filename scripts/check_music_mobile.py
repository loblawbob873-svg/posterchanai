#!/usr/bin/env python3
"""The music player as a phone/tablet app — driven at real widths.

    venv-unified/bin/python scripts/check_music_mobile.py [base_url]

The generic check (check_client_mobile.py) never opens this surface, and the player is not a normal
view: it is a fixed-position panel the client mounts on <html>, so nothing else in the app tests it.

Assertions, each one a way "use it as your music player on a phone" fails:

  not-full-screen   Expanded, the player is a small card floating over the feed — a scaled-down
                    desktop window on a device where the app IS the screen. It must fill it.
  it-bounces        `mpwiggle` rocks the whole box while a track plays. Charming on a 338px window
                    parked in a corner; motion sickness on a full screen you are reading a track
                    list on.
  list-cannot-scroll  The panel scrolls instead of the list inside it, or the list drags the page
                    behind it (overscroll chaining) — on a phone that reads as the app fighting you.
  tap-targets       Controls or track rows too small to hit with a thumb.
  car-controls      A media-session handler a car head unit needs is missing (its button is then
                    dead — the platform will not synthesise one), or the position update throws
                    before a track has loaded, which takes the title and artwork with it.
  offscreen         The panel is positioned off the edge — the desktop drag writes inline left/top,
                    and a window narrowed afterwards would keep them.
  mini-not-floating The MINI bar must stay a floating pill above the bottom nav: that one is meant
                    to sit over whatever you are doing, and full-screening it would trap you.

Exit 0 = clean, 1 = problems (printed), 2 = could not run (no Chrome / site unreachable).
"""
import os
import asyncio
import json
import shutil
import subprocess
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3051"
WIDTHS = [(390, 844, "phone"), (820, 1180, "tablet"), (1400, 900, "desktop")]
PORT = int(os.environ.get("PC_CHECK_PORT") or 9494)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-music-check"

# The player mounts itself on demand; nothing in a guest session opens it, so the harness builds the
# real markup through the client's own MusicPlayer and inspects what the CSS does to it.
DRIVE = r"""(async () => {
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  const P = window.__PC;
  if (!P || !P.MusicPlayer) return { skip: 'MusicPlayer is not exposed' };
  const MP = P.MusicPlayer;
  let el = MP.ensure();
  el.classList.remove('hidden');
  MP.setMin(false);
  MP._render();
  await sleep(120);
  const r = el.getBoundingClientRect();
  const cs = getComputedStyle(el);
  const list = el.querySelector('.mp-list');
  const lcs = list ? getComputedStyle(list) : null;
  const big = el.querySelector('.mp-controls .mp-big');
  const seek = el.querySelector('.mp-seek');
  const out = {
    w: Math.round(r.width), h: Math.round(r.height),
    left: Math.round(r.left), top: Math.round(r.top), right: Math.round(r.right),
    vw: window.innerWidth, vh: window.innerHeight,
    anim: cs.animationName,
    playingAnim: (() => { el.classList.add('playing');
      const a = getComputedStyle(el).animationName; el.classList.remove('playing'); return a; })(),
    listOverflow: lcs ? lcs.overflowY : '', listOverscroll: lcs ? lcs.overscrollBehaviorY : '',
    listMaxH: lcs ? lcs.maxHeight : '',
    bigH: big ? Math.round(big.getBoundingClientRect().height) : 0,
    seekH: seek ? Math.round(seek.getBoundingClientRect().height) : 0,
    trackH: (() => { const t = el.querySelector('.mp-track');
                     return t ? Math.round(t.getBoundingClientRect().height) : 0; })(),
  };
  /* CAR / HEADSET CONTROLS. A head unit's buttons are dead unless a handler is registered for
     them — the platform does not synthesise one — and its scrubber and elapsed time come from
     setPositionState, not from the audio element, which the OS never sees. */
  {
    const got = [];
    const pos = [];
    const ms = navigator.mediaSession;
    if (ms) {
      const realSet = ms.setActionHandler && ms.setActionHandler.bind(ms);
      ms.setActionHandler = (a, fn) => { got.push(a); try { realSet && realSet(a, fn); } catch(_){} };
      if (ms.setPositionState) { const rp = ms.setPositionState.bind(ms);
        ms.setPositionState = (st) => { pos.push(st); try { rp(st); } catch(_){} }; }
    }
    // ensure() registers them once, on the first build — so rebuild to observe it.
    const old = document.getElementById('music-player'); if (old) old.remove();
    MP.el = null; MP.ensure();
    el = MP.el;                       // the old node was removed; measure the one that exists now
    out.actions = got;
    // _media() must not throw before a track has loaded (duration is NaN there, and
    // setPositionState rejects that outright — an unguarded call takes the whole update with it).
    let threw = false; try { MP._media(); } catch(_) { threw = true; }
    out.mediaThrew = threw;
    out.positionCalls = pos.length;
    el.classList.remove('hidden'); MP.setMin(false); MP._render();
    await sleep(60);
  }
  // …and the mini bar, which must NOT become full screen.
  MP.setMin(true); MP._render(); await sleep(80);
  const m = el.getBoundingClientRect();
  out.miniW = Math.round(m.width); out.miniH = Math.round(m.height);
  out.miniTop = Math.round(m.top);
  out.miniCls = el.className;
  out.miniBottomGap = Math.round(window.innerHeight - m.bottom);
  MP.setMin(false);

  /* THE MUSIC APP's transport — a different surface from the floating widget, and for a long time
     the one with no scrubber at all: prev / play / next and nothing to move through a track with.
     It is also where .ma-now WRAPS on a phone, which is how a seek bar ends up as a 40px stub
     sharing a line with four buttons. Measured here rather than reasoned about. */
  try {
    if (P.renderMusicApp) {
      P.renderMusicApp();
      await sleep(150);
      const bar = document.getElementById('ma-seek');
      out.appSeek = !!bar;
      if (bar) {
        const br = bar.getBoundingClientRect();
        const row = bar.parentElement.getBoundingClientRect();
        out.appSeekW = Math.round(br.width);
        out.appSeekH = Math.round(br.height);
        out.appSeekRowW = Math.round(row.width);
        // A drag that the browser steals as a page scroll stops sending pointermove mid-gesture.
        out.appSeekTouch = getComputedStyle(bar).touchAction;
        out.appSeekFill = !!bar.querySelector('.mp-seek-fill');
        out.appSeekRole = bar.getAttribute('role') || '';
        out.appSeekFocusable = bar.tabIndex >= 0;
        out.appSeekOverflow = document.documentElement.scrollWidth > window.innerWidth + 1;
        // Both times must be present, or the bar is a position with no scale to read it against.
        out.appTimes = !!(document.getElementById('ma-cur') && document.getElementById('ma-dur'));
        const eq = document.getElementById('ma-eq');
        out.appEq = !!eq;
        out.appEqBands = eq ? eq.querySelectorAll('input[data-band]').length : 0;
        out.appEqPresets = eq ? eq.querySelectorAll('.ma-eq-preset').length : 0;
        const ctl = document.querySelector('.ma-ctl');
        out.appTransportOverflow = ctl ? ctl.scrollWidth > ctl.clientWidth + 1 : false;
      }
    }
  } catch (e) { out.appErr = String(e && e.message || e); }

  return out;
})()"""


async def drive(url):
    import websockets  # noqa: F401
    subprocess.run(["rm", "-rf", PROFILE], check=False)
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium"))
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    problems = []
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
        import websockets
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
                    print("  DEBUG:", json.dumps(r["exceptionDetails"])[:500])
                    return None
                return r["result"].get("value")

            await call("Runtime.enable")
            await call("Page.enable")
            for w, h, label in WIDTHS:
                await call("Emulation.setDeviceMetricsOverride",
                           {"width": w, "height": h, "deviceScaleFactor": 2 if w < 900 else 1,
                            "mobile": w < 900})
                await call("Page.navigate", {"url": url})
                ok = False
                for _ in range(80):
                    await asyncio.sleep(0.25)
                    if await js("!!(window.__PC && window.__PC.MusicPlayer)"):
                        ok = True
                        break
                if not ok:
                    print("SKIP  the client never exposed MusicPlayer")
                    return 2
                r = await js(DRIVE, awaited=True)
                if not r:
                    problems.append((label, "harness", "the player did not evaluate"))
                    continue
                if os.environ.get("PC_DEBUG"):
                    print(f"  DEBUG {label}: {json.dumps(r, sort_keys=True)}")
                if r.get("skip"):
                    print("SKIP  " + r["skip"])
                    return 2
                # Car controls are not a phone question — a Bluetooth head unit is driven from any
                # build — so this is asserted at every width.
                for want in ("play", "pause", "previoustrack", "nexttrack",
                             "seekto", "seekbackward", "seekforward", "stop"):
                    if want not in (r.get("actions") or []):
                        problems.append((label, "car-controls",
                                         f"no media-session handler for {want!r} — that button is "
                                         f"dead on a car head unit (registered: {r.get('actions')})"))
                if r.get("mediaThrew"):
                    problems.append((label, "car-controls",
                                     "the media update threw before a track loaded — setPositionState "
                                     "rejects a NaN duration and takes the metadata with it"))
                phone = w <= 820
                if phone:
                    if r["w"] < r["vw"] - 2 or r["h"] < r["vh"] - 2:
                        problems.append((label, "not-full-screen",
                                         f"the expanded player is {r['w']}x{r['h']} in a "
                                         f"{r['vw']}x{r['vh']} screen — still a floating card"))
                    if r["playingAnim"] not in ("none", ""):
                        problems.append((label, "it-bounces",
                                         f"the full-screen player animates ({r['playingAnim']}) while playing"))
                    if r["listOverflow"] not in ("auto", "scroll"):
                        problems.append((label, "list-cannot-scroll",
                                         f"the track list has overflow-y:{r['listOverflow']}"))
                    if r["listOverscroll"] != "contain":
                        problems.append((label, "list-cannot-scroll",
                                         "flicking the track list drags the page behind it "
                                         f"(overscroll-behavior-y:{r['listOverscroll']})"))
                    if r["bigH"] < 44:
                        problems.append((label, "tap-targets",
                                         f"play button is {r['bigH']}px — under the 44px a thumb needs"))
                    if r["trackH"] and r["trackH"] < 40:
                        problems.append((label, "tap-targets", f"track rows are {r['trackH']}px"))
                    if r["seekH"] < 8:
                        problems.append((label, "tap-targets", f"the scrubber is {r['seekH']}px tall"))
                    if r["left"] < -1 or r["right"] > r["vw"] + 1:
                        problems.append((label, "offscreen",
                                         f"the panel sits at left={r['left']} right={r['right']}"))
                    # The minimised player carries BOTH classes (`class="mp mp-mini"`), so a
                    # full-screen rule written as bare `#music-player.mp` swallows the pill as well.
                    # When that happened the bar measured 382x768 on a 390x844 screen — WIDTH alone
                    # let it through (382 < 388), which is why this now asks the questions that
                    # actually separate a pill from a panel: how TALL it is, and whether it starts
                    # at the top of the screen.
                    if r["miniH"] > 120 or r["miniTop"] < r["vh"] / 2:
                        problems.append((label, "mini-not-floating",
                                         f"the MINI bar is {r['miniW']}x{r['miniH']} at "
                                         f"top={r['miniTop']} (classes {r['miniCls']!r}) — "
                                         "that is a panel, not a pill"))
                    if r["miniW"] >= r["vw"] - 2:
                        problems.append((label, "mini-not-floating",
                                         "the MINI bar went full-width; it is meant to float"))
                    if r["miniBottomGap"] < 40:
                        problems.append((label, "mini-not-floating",
                                         f"the mini bar sits {r['miniBottomGap']}px from the bottom — "
                                         "under the mobile nav"))
                else:
                    # Desktop keeps the floating window, wiggle and all — that is the design there.
                    if r["w"] >= r["vw"] - 2:
                        problems.append((label, "not-full-screen",
                                         "the desktop player went full-screen; it is a window there"))

                # ---- the Music app's scrubber, at every width ----------------------------------
                if r.get("appErr"):
                    problems.append((label, "app-broken", f"renderMusicApp threw: {r['appErr']}"))
                elif not r.get("appSeek"):
                    problems.append((label, "no-app-scrubber",
                                     "the Music app has no seek bar — prev/play/next is not a way "
                                     "to move through a track"))
                else:
                    if not r.get("appEq") or r.get("appEqBands") != 3 or r.get("appEqPresets", 0) < 4:
                        problems.append((label, "equalizer-missing",
                                         "the unified player does not expose three EQ bands and presets"))
                    if r.get("appTransportOverflow"):
                        problems.append((label, "transport-overflow",
                                         "the unified transport controls overflow their row"))
                    if not r.get("appSeekFill"):
                        problems.append((label, "no-app-scrubber",
                                         "the seek bar has no fill element, so it can show no position"))
                    if not r.get("appTimes"):
                        problems.append((label, "no-app-scrubber",
                                         "elapsed/duration are missing — a position with no scale"))
                    # A bar you aim with has to be big enough to hit, and wide enough to aim ALONG.
                    # 24px is the phone minimum for a target; the width one is what catches the bar
                    # being squeezed to a stub by the buttons when .ma-now wraps.
                    if r["appSeekH"] < (14 if phone else 8):
                        problems.append((label, "tap-targets",
                                         f"the Music app scrubber is {r['appSeekH']}px tall"))
                    if r["appSeekW"] < r["appSeekRowW"] * 0.55:
                        problems.append((label, "app-scrubber-squeezed",
                                         f"the scrubber is {r['appSeekW']}px in a {r['appSeekRowW']}px "
                                         "row — it is sharing a line instead of owning one"))
                    if r.get("appSeekTouch") not in ("none", "pinch-zoom"):
                        problems.append((label, "drag-stolen",
                                         f"touch-action is {r.get('appSeekTouch')!r} — the browser "
                                         "takes the drag as a page scroll and pointermove stops"))
                    if r.get("appSeekRole") != "slider" or not r.get("appSeekFocusable"):
                        problems.append((label, "app-scrubber-unreachable",
                                         "the scrubber is not a focusable slider — no keyboard seek"))
                    if r.get("appSeekOverflow"):
                        problems.append((label, "horizontal-overflow",
                                         "the Music app scrolls sideways with the seek row in it"))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        shutil.rmtree(PROFILE, ignore_errors=True)

    if problems:
        print(f"FAIL  {len(problems)} problem(s):")
        for label, kind, msg in problems:
            print(f"  [{label}] {kind}: {msg}")
        return 1
    print("OK  music player works at phone, tablet and desktop widths")
    return 0


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    try:
        with urllib.request.urlopen(BASE + "/client", timeout=8) as r:
            if r.status != 200:
                raise RuntimeError(r.status)
    except Exception as e:
        print(f"SKIP  {BASE}/client is not reachable ({e})")
        return 2
    return asyncio.run(drive(BASE + "/client"))


if __name__ == "__main__":
    sys.exit(main())
