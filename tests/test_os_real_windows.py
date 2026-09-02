"""A PosterChan WINDOW IS A COMPOSITOR WINDOW — stage 1, the container.

Reported as "telegram on desktop is swallowing windows and its not separating", and asked for
directly: "i want PosterChanOS to behave like a regular window manager".

It cannot today, and no threshold fixes it. The sway config floats every app and un-floats the
shell, so the desktop is ONE tiled window with every native app above it — sway always paints
floating over tiled, so a PosterChan window can never be drawn in front of Telegram. "Click to bring
it forward" is therefore faked by taking the native surface off the screen and leaving a frozen
screenshot in its frame. That fake produced three separate reports in one day, and its worst
property is the arithmetic: a NINE PIXEL conflict costs the whole application, where a real window
manager would leave the other 99% visible and live.

So a window becomes a real toplevel. sway stacks it natively, clicking raises exactly it, and the
entire parking subsystem has nothing left to do.

WHAT MAKES IT AFFORDABLE, and what these tests mostly protect: the window is opened same-origin, so
it shares the opener's process and its JavaScript objects. It uses the DESKTOP's Store, relay pool
and signer through `window.opener` — there is no IPC engine/view split and no second relay pool per
window. Two full copies already exist (one per monitor); this must not make that worse.

WEB AND ANDROID DO NOT CHANGE. A browser tab cannot make OS windows. This is a second backend behind
one API, and the two being fed from one list is what stops them drifting.

Stage 1 is the CONTAINER ONLY, deliberately: the last desktop rewrite that skipped straight to
behaviour produced a black screen, and this one is landing on a machine somebody is using.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OSWIN = ROOT / "static/js/client/oswin.js"
APP_JS = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
MAIN_JS = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
SWAY = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config").read_text(encoding="utf-8")
PAGE = (ROOT / "templates/client.html").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
NODE = shutil.which("node") or shutil.which("nodejs")


def run_js(body: str):
    """Drive the SHIPPED oswin.js against a stubbed window."""
    src = rf"""
      const path = {json.dumps(str(OSWIN))};
      const make = (over) => {{
        const w = Object.assign({{
          location: {{ search: '', pathname: '/client' }},
          localStorage: {{ _v: {{}}, getItem(k){{ return this._v[k] ?? null; }},
                           setItem(k,v){{ this._v[k]=String(v); }} }},
          /* `open()` refuses a view the nav does not know (a window that cannot show what it
             says is worse than no window), so the stub has to model that nav. */
          document: {{ title:'', documentElement:{{ classList:{{ add(){{}} }} }},
                       querySelector(sel){{
                         const m = /^\.nav-item\[data-view="(.*)"\]$/.exec(sel);
                         const nav = this._nav || ['home','global','notes','mail','settings'];
                         return m ? (nav.indexOf(m[1]) >= 0 ? {{ dataset:{{ view:m[1] }} }} : null) : null;
                       }} }},
          open(){{ return null; }},
        }}, over || {{}});
        delete require.cache[require.resolve(path)];
        const prev = global.globalThis;
        w.PCOSWin = undefined;
        const mod = (function(){{
          const g = w; g.module = {{exports:{{}}}};
          const fn = new Function('globalThis','module', require('fs').readFileSync(path,'utf8'));
          fn(g, g.module); return g.module.exports;
        }})();
        return {{ w, api: mod }};
      }};
      const out = {{}};
      {body}
      process.stdout.write(JSON.stringify(out));
    """
    done = subprocess.run([NODE, "-e", src], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-1500:]
    return json.loads(done.stdout)


pytestmark = pytest.mark.skipif(not NODE, reason="needs node")


# ------------------------------------------------------------------ where it is allowed to happen

def test_a_browser_never_opens_a_compositor_window():
    """The promise that the UI stays identical on the web. `pcWM` is the compositor bridge and only
    the PosterChanOS shell has it, so everywhere else `open()` answers null and the caller falls
    back to the in-page window os.js has always drawn."""
    got = run_js("""
      const a = make({});                                   // a browser: no pcWM
      a.w.localStorage.setItem('pc_os_toplevels','1');
      out.browser = a.api.enabled();
      const b = make({ pcWM: {} });                         // the shell, nothing set
      out.shellDefault = b.api.enabled();
      const c = make({ pcWM: {} });
      c.w.localStorage.setItem('pc_os_toplevels','0');      // one machine opting out
      out.shellOptedOut = c.api.enabled();
    """)
    assert got["browser"] is False, "a browser tab would try to open an OS window"
    assert got["shellDefault"] is True, (
        "real windows are off by default again — the shell is one TILED sway window and every "
        "native app floats above it, so without this a PosterChan window can never be raised over "
        "Firefox or Telegram ('terminal gets fucked by telegram and firefox, can never get focus')")
    assert got["shellOptedOut"] is False, (
        "pc_os_toplevels=0 no longer turns it off — there has to be one key back to the old "
        "behaviour on a machine somebody depends on")


def test_a_window_does_not_open_windows():
    """The desktop opens windows. A window that opened windows would nest the whole desktop model
    inside itself, and each child's `window.opener` would point at a view rather than the client."""
    got = run_js("""
      const a = make({ pcWM: {}, location:{ search:'?pcwin=notes', pathname:'/client' } });
      a.w.localStorage.setItem('pc_os_toplevels','1');
      out.enabled = a.api.enabled();
      out.isWindow = a.api.isWindow();
    """)
    assert got["isWindow"] is True
    assert got["enabled"] is False


def test_open_returns_null_rather_than_throwing_when_it_cannot():
    """The caller uses the answer to decide whether to fall back, so this must never throw."""
    got = run_js("""
      const a = make({ pcWM: {}, open(){ throw new Error('refused'); } });
      a.w.localStorage.setItem('pc_os_toplevels','1');
      out.opened = a.api.open('notes','Notes');
    """)
    assert got["opened"] is None


def test_the_url_carries_the_view_and_stays_same_origin():
    """Same-origin is the whole mechanism: it is what puts the child in the opener's process and
    makes `window.opener` a live reference to the desktop's client rather than a message channel."""
    got = run_js("""
      let seen = null;
      const a = make({ pcWM: {}, location:{ search:'', pathname:'/client' },
                       open(u,n,f){ seen = {u,n,f}; return { }; } });
      a.w.localStorage.setItem('pc_os_toplevels','1');
      a.api.open('notes','Notes',{width:900,height:600});
      out.seen = seen;
    """)
    assert got["seen"]["u"].startswith("/client?"), got["seen"]
    assert "pcwin=notes" in got["seen"]["u"]
    assert "://" not in got["seen"]["u"], "the child must not be cross-origin"
    assert "width=900" in got["seen"]["f"] and "height=600" in got["seen"]["f"]


# ------------------------------------------------------------------ the desktop it belongs to

def test_a_dead_desktop_is_reported_not_thrown():
    """The opener can be CLOSED while this window is still up — the desktop crashed, or its renderer
    was rebuilt under memory pressure. Reading a dead one throws, and a window whose desktop has
    gone is alone, not broken."""
    got = run_js("""
      out.none    = make({ location:{search:'?pcwin=x',pathname:'/c'} }).api.desktop();
      out.closed  = make({ location:{search:'?pcwin=x',pathname:'/c'}, opener:{ closed:true, __PC:{} } }).api.desktop() === null;
      out.noClient= make({ location:{search:'?pcwin=x',pathname:'/c'}, opener:{ closed:false } }).api.desktop() === null;
      const live = { closed:false, __PC:{ marker:'engine' } };
      out.live    = make({ location:{search:'?pcwin=x',pathname:'/c'}, opener:live }).api.desktop() === live;
      // The getter is installed AFTER the stub is built: Object.assign would otherwise invoke it
      // while copying, and the throw would come from the fixture rather than from desktop().
      let threw = false;
      const dead = make({ location:{search:'?pcwin=x',pathname:'/c'} });
      Object.defineProperty(dead.w, 'opener', { get(){ throw new Error('gone'); } });
      try{ dead.api.desktop(); }catch(_){ threw = true; }
      out.throwsOnDeadOpener = threw;
    """)
    assert got["none"] is None and got["closed"] and got["noClient"] and got["live"]
    assert got["throwsOnDeadOpener"] is False, "reading a dead opener escaped as an exception"


def test_adopt_names_the_window_so_the_compositor_can_tell_it_apart():
    """sway keys its floating rule on the TITLE, because a same-origin child shares the desktop's
    app_id — and that sharing is exactly what makes the design cheap. Get the title wrong and the
    window TILES: it would take the desktop's place on screen."""
    got = run_js("""
      const titles = [];
      const a = make({ location:{search:'?pcwin=notes',pathname:'/c'},
                       document:{ set title(v){ titles.push(v); }, get title(){ return titles[titles.length-1]||''; },
                                  documentElement:{ classList:{ add(c){ out.cls = c; } } } },
                       opener:{ closed:false, __PC:{} } });
      out.state = a.api.adopt();
      out.title = titles[titles.length-1];
      out.prefix = a.api.TITLE;
    """)
    assert got["title"].startswith(got["prefix"]), got
    assert "notes" in got["title"]
    assert got["cls"] == "pc-oswin"
    assert got["state"]["shared"] is True


def test_the_sway_rule_matches_that_exact_title_and_comes_last():
    """Ordering is load-bearing and the file says so: later rules win, and the un-float rules for
    the desktop are above. A float rule placed before them does nothing at all."""
    assert 'title="^PosterChan Window"' in SWAY
    float_at = SWAY.index('title="^PosterChan Window"')
    unfloat_at = SWAY.rindex('floating disable, border none')
    assert float_at > unfloat_at, (
        "the window float rule is above the desktop's un-float rules, so it loses and every "
        "PosterChan window would tile over the desktop")


def test_the_title_the_client_sets_is_the_title_sway_matches():
    """One string, two files. They were separate constants for about a minute, which is how a rule
    that looks right matches nothing."""
    js_title = re.search(r"const TITLE = '([^']+)'", OSWIN.read_text(encoding="utf-8"))
    assert js_title, "oswin.js no longer declares its window title"
    assert f'title="^{js_title.group(1)}"' in SWAY


# ------------------------------------------------------------------ the Electron side

def test_the_shell_makes_a_window_frameless_and_keeps_the_bridges():
    """Frameless because the client draws the SAME title bar it draws on the web — that is the
    promise that the UI is identical. The preload has to be the same one, or `pcWM`, the signer and
    the clipboard are missing from every window."""
    handler = MAIN_JS[MAIN_JS.index("setWindowOpenHandler"):]
    handler = handler[:handler.index("will-redirect")]
    assert "pcwin=" in handler, "the shell no longer recognises a window URL"
    assert "frame: false" in handler
    assert "preload.js" in handler


def test_a_window_never_claims_folder_sync_ownership():
    """`--pc-secondary-surface` is how the preload withholds background work. A window is a VIEW
    onto the desktop's client; a second writer over one synced tree is the failure that marker was
    added to prevent, and it would now be one per open window."""
    handler = MAIN_JS[MAIN_JS.index("setWindowOpenHandler"):]
    handler = handler[:handler.index("will-redirect")]
    # The ARRAY, not the word: the comment above it names both flags, so matching prose let the
    # real argument be deleted with this test still green. (Caught by mutating it.)
    args = re.search(r"additionalArguments:\s*\[([^\]]*)\]", handler)
    assert args, "the window no longer passes any preload arguments"
    assert "'--pc-secondary-surface'" in args.group(1), (
        "a window would claim folder-sync ownership — one background writer per open window over "
        "the same synced tree")
    assert "'--pc-preload-dir=' + __dirname" in args.group(1), (
        "the preload cannot find its siblings without this")


# ------------------------------------------------------------------ the client side

def test_a_window_boots_the_client_but_never_the_desktop():
    """STAGE 2. A window runs the real client — that is what makes its view byte-identical to the
    web — but it must not `PCOS.restore()`, which turns the page into the windowed shell. Doing
    that inside a window would draw a whole second desktop, icons and taskbar, inside it."""
    boot = APP_JS[APP_JS.index("  async function boot(){"):]
    boot = boot[:boot.index("// ---------- auth UI ----------")]
    assert "PCOSWin.isWindow()" in boot and "_asWindow = true" in boot
    restore = [l for l in boot.splitlines() if "PCOS.restore()" in l]
    assert restore, "the desktop restore has moved — re-read this test"
    assert all("!_asWindow" in l for l in restore), (
        "a window would restore the desktop shell inside itself: " + restore[0].strip()[:90])


def test_a_window_lands_on_the_view_it_was_opened_for():
    """Without this it lands on the timeline like any other page, and every window is the same
    window. Gated on isWindow() so no existing boot path moves — a speculative landing guard once
    broke the APK, because applyInstanceGating can switchView during boot."""
    route = APP_JS[APP_JS.index("  async function routeFromPath(){"):]
    route = route[:route.index("const e = _entityFromPath();")]
    assert "_inWin()" in route and "PCOSWin.viewOf()" in route
    assert "switchView(v)" in route


def test_a_window_shows_no_sidebar_desktop_or_mobile_nav():
    """One view per window: the window manager is how you reach the others now."""
    for sel in ("html.pc-oswin .sidebar", "html.pc-oswin .mobilenav", "html.pc-oswin #os-root"):
        assert sel in CSS, f"{sel} is not hidden in a window"


def test_the_window_boot_cannot_take_the_desktop_down_with_it():
    """This runs before everything. A throw here on the DESKTOP path would be a black screen, which
    is precisely how the last speculative change to a boot path failed."""
    boot = APP_JS[APP_JS.index("  async function boot(){"):]
    boot = boot[:boot.index("_bootCfg")]
    guard = boot[boot.index("PCOSWin"):]
    assert "catch" in guard[:400], "the window-mode check is not guarded"


def test_the_page_loads_oswin_before_the_client():
    """boot() asks PCOSWin whether this document is a window, so it has to exist by then."""
    assert "oswin.js" in PAGE
    assert PAGE.index("oswin.js") < PAGE.index("client/app.js")


def test_the_window_only_hides_chrome_and_never_the_view():
    """The window CSS is allowed to remove the shell's furniture and nothing else. Hiding a content
    container here would be a blank window with no error — the same shape as the unstyled office
    sheet earlier, one file over."""
    block = CSS[CSS.index("html.pc-oswin .sidebar"):]
    block = block[:block.index(".oswin-probe")]
    hidden = re.findall(r"html\.pc-oswin ([#.][a-z0-9-]+)", block)
    assert hidden, "the window stylesheet no longer hides anything"
    for sel in hidden:
        assert sel in (".sidebar", ".mobilenav", "#os-root", "#app", ".main"), (
            f"a window hides {sel}, which is not shell furniture — check it is not the view itself")


def test_a_window_is_not_given_the_desktops_remembered_view():
    """MEASURED ON REAL HARDWARE, twice, and the reason the first two attempts looked like the
    window "ignoring" its view: it opened for `notes`, reported `viewOf: notes`, and then reported
    `VIEW: global`.

    A window shares the desktop's saved state, and the restore path switches to the REMEMBERED view
    (`st.pcv`). So every window briefly showed what it was asked for and then switched itself to
    whatever the desktop had last been looking at — one window's worth of content, repeated."""
    src = APP_JS[APP_JS.index("if(!_inWin() && !_entityFromPath() && st && st.pcv"):]
    src = src[:src.index("_restoreNavScroll(st);")]
    assert "switchView(st.pcv)" in src, "the remembered-view restore has moved — re-read this test"
    guard = APP_JS[APP_JS.index("function _inWin(){"):]
    guard = guard[:guard.index("\n")]
    assert "PCOSWin.isWindow()" in guard and "catch" in guard, (
        "the window check is unguarded — a throw here runs during boot on every client")


def test_the_window_identity_survives_the_url_being_rewritten():
    """THE OTHER HALF, also measured on hardware: `location.search` was EMPTY a second after the
    window opened, because the client rewrites its own URL while routing. Asked of the URL, the
    window then reported that it was not a window — it kept its title and landed on the timeline.
    The answer is latched the first time it is asked, before anything can navigate."""
    src = OSWIN.read_text(encoding="utf-8")
    fn = src[src.index("function isWindow(){"):src.index("function viewOf(){")]
    assert "__PC_WIN_STATE__" in fn, "isWindow() reads only the URL again"
    assert "_latch()" in fn, "nothing records the answer before the URL can change"
    view = src[src.index("function viewOf(){"):src.index("function _latch(){")]
    assert view.index("__PC_WIN_STATE__") < view.index("URLSearchParams"), (
        "viewOf() prefers the URL over the latch, so it goes blank on the first navigation")


def test_the_start_view_preference_does_not_reach_a_window():
    """THE THIRD of three places that set a view during boot, and the one that actually won.

    It applies the startup-view PREFERENCE, guarded only on "is this page the desktop" — and a
    window is not the desktop, so it qualified. Measured on hardware three times: windows opened
    for notes, files and music, each reporting the right `viewOf` and `VIEW: global`.

    All three now exclude a window, and this asserts all three together — fixing two of them is
    indistinguishable from fixing none."""
    starts = APP_JS[APP_JS.index("const _win = _inWin() ? PCOSWin.viewOf() : '';"):]
    assert "switchView(_startView())" in starts[:1600], "the preference path is gone entirely"
    assert "try{ switchView(_win); }catch(_){}" in starts[:1600], (
        "a window no longer switches to the view it was opened for")
    for where, needle in (
            ("the startup preference", "const _win = _inWin() ? PCOSWin.viewOf() : '';"),
            ("the remembered view", "if(!_inWin() && !_entityFromPath() && st && st.pcv"),
            ("the path route", "if(_inWin()){ const v = PCOSWin.viewOf();")):
        assert needle in APP_JS, f"{where} no longer excludes a window"
    # One helper, guarded once — three copies of the same try/catch is how two of them drift.
    assert "function _inWin(){" in APP_JS and "catch(_){ return false; }" in APP_JS


def test_suppressing_the_other_paths_is_not_enough_on_its_own():
    """The failure this cost two rounds to find: guarding every path that sets a view left NOTHING
    setting one, so `VIEW` stayed at its initial value and every window showed the timeline anyway.
    The window must be GIVEN its view, not merely spared the others."""
    block = APP_JS[APP_JS.index("const _win = _inWin() ? PCOSWin.viewOf() : '';"):]
    block = block[:block.index("_onLandingView = true; }") + 24]
    assert "PCOSWin.viewOf()" in block, "the window is no longer told which view to show"
    assert "switchView(_win)" in block
    assert block.index("switchView(_win)") < block.index("switchView(_startView())"), (
        "the preference is applied before the window's own view, so it wins")


# ------------------------------------------------------------------ stage 3: the pop-out

OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")


def _popout():
    return OS_JS[OS_JS.index("  function popOut(w){"):OS_JS.index("  function openApp(view, label")]


def test_a_window_can_be_handed_to_the_compositor():
    """STAGE 3, opt-in. The in-page frame carries the folder, EXTRAS, Messages-tab and
    monitor-handoff machinery; moving all of it at once is how a desktop rewrite produces a black
    screen. This moves ONE window and changes nothing else."""
    body = _popout()
    assert "PCOSWin.open(" in body
    assert "closeWin(w)" in body, "the in-page frame is left behind, so the window exists twice"


def test_the_frame_survives_a_pop_out_that_did_not_open():
    """`open()` answers null for every reason it cannot — no compositor, the flag off, a refused
    window. Closing the frame first would destroy the only copy of what somebody was looking at."""
    body = _popout()
    assert body.index("if(!child)") < body.index("closeWin(w)"), (
        "the frame is closed before the new window is known to exist")
    assert "toast(" in body, "a refused pop-out says nothing"


def test_the_control_is_absent_where_it_could_not_work():
    """A browser tab and the APK have no such thing as a window. A button that does nothing is
    worse than no button — that is the same lesson as the office save sheet's dead classes."""
    wiring = OS_JS[OS_JS.index("const pop = $('.osw-b[data-w=\"pop\"]', el);"):]
    wiring = wiring[:wiring.index("const maxBtn")]
    assert "PCOSWin.enabled()" in wiring
    assert "pop.remove()" in wiring


def test_it_pops_the_view_the_window_is_actually_showing():
    """`appView` is the tab a Messages frame is on; `view` is the frame's identity. Popping the
    frame identity would open Direct Messages for a window showing Communities."""
    rule = OS_JS[OS_JS.index("  function popOutView(w){"):OS_JS.index("  function popOut(w){")]
    assert "w.appView || w.view" in rule
    assert "popOutView(w)" in _popout(), "popOut no longer goes through the shared rule"


def test_a_document_window_is_refused_rather_than_stripped():
    """THIS TEST USED TO ASSERT THE BUG. It demanded `replace(/^doc:/, '')` on the reasoning that
    "a document window's view keeps its `doc:` prefix, which routes to nothing" — the observation
    was right and the remedy was backwards. Stripping it does not make `doc:os-settings` routable,
    it makes it `os-settings`, which routes to nothing either — and `switchView` does not validate,
    so the window opened on the DEFAULT TIMELINE under the title "System Settings". Reported as
    "Systems settings just loaded a social feed"."""
    rule = OS_JS[OS_JS.index("  function popOutView(w){"):OS_JS.index("  function popOut(w){")]
    assert "replace(/^doc:/" not in rule, "the doc: prefix is being stripped again"
    assert "'doc:'" in rule and "return ''" in rule, (
        "a doc: window is no longer refused a pop-out")


# STAGE 4 (OPENING EVERY APP AS A TOPLEVEL) IS REVERTED — it broke the terminal.
#
# `openApp` asked PCOSWin for a real window first and returned when it got one, so no in-page frame
# was built. Reported as "terminal don't even work", and sway's tree agreed: no `PosterChan Window`
# existed, so the app had been taken off the old path without arriving on the new one. The per-window
# ⧈ route stays; making it the default for every launch needs one real window measured on hardware
# first. The tests below still cover the parts that ARE shipped.


def test_the_window_maps_with_the_title_sway_floats_on():
    """THE MAP MOMENT, which is the only one sway's rule ever sees.

    `for_window` is evaluated when a surface maps. The rule that floats these windows keys on the
    TITLE — it has to, because the app_id is shared with the desktop, which must stay tiled. The
    page sets that title in `adopt()`, and `adopt()` runs after the document loads, which is after
    the map. So the title must also be on the BrowserWindow at creation, or the first app opened
    maps under Electron's default title, matches nothing, and is tiled into the shell's layout —
    splitting the desktop in half.
    """
    main_js = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
    handler = main_js[main_js.index("setWindowOpenHandler"):]
    handler = handler[:handler.index("return { action: 'deny' };")]
    assert "title: 'PosterChan Window'" in handler, (
        "a real window maps without the title sway floats on — it would be tiled into the desktop")

    sway = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config").read_text(encoding="utf-8")
    rule = [l for l in sway.splitlines() if "PosterChan Window" in l and "floating enable" in l]
    assert rule, "sway no longer floats these windows by title"

    oswin = (ROOT / "static/js/client/oswin.js").read_text(encoding="utf-8")
    assert "const TITLE = 'PosterChan Window'" in oswin, (
        "the page's title and the compositor rule have drifted apart")


def test_a_terminal_window_arms_its_own_shell():
    """THE REASON THIS WAS REVERTED ONCE, and the reason it can ship now.

    `openTerminalHere()` runs `PCTerm.openLocal()` and THEN opens the app. With the terminal in its
    own window the PTY was armed in the DESKTOP's page while the terminal rendered in a different
    one, which has its own PCTerm and knew nothing about it. Measured on the machine at the time:
    `PosterChan Window — terminal`, floating, correctly titled — and empty. Reported as "terminal
    don't even work", and the window manager was never the problem.

    A window arms its own shell on landing, which also survives it being reloaded or restored."""
    at = APP_JS.index("const _win = _inWin() ? PCOSWin.viewOf() : '';")
    landing = APP_JS[at:at + 1600]
    assert "_win === 'terminal'" in landing, (
        "a terminal window renders with no local shell armed — a real window containing nothing")
    assert "PCTerm.openLocal()" in landing
    assert landing.index("_win === 'terminal'") < landing.index("switchView(_win)"), (
        "the shell is armed after the view is switched, so the first render has no session")


def test_opening_an_app_asks_for_a_real_window_first():
    body = OS_JS[OS_JS.index("  function openApp(view, label, icon, render, noFeed, direct){"):]
    body = body[:body.index("const existing = wins.find")]
    assert "PCOSWin.open(" in body and "PCOSWin.enabled()" in body
    assert "popOutView(" in body, "an EXTRA or doc: frame could be handed a window it cannot fill"
    assert "if(real) return null;" in body, "a refused window must fall back to the in-page frame"
    assert "if(!direct){" in body, "a managed re-open must not be turned into a toplevel"
