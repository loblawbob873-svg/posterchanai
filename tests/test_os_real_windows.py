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
    src = f"""
      const path = {json.dumps(str(OSWIN))};
      const make = (over) => {{
        const w = Object.assign({{
          location: {{ search: '', pathname: '/client' }},
          localStorage: {{ _v: {{}}, getItem(k){{ return this._v[k] ?? null; }},
                           setItem(k,v){{ this._v[k]=String(v); }} }},
          document: {{ title:'', documentElement:{{ classList:{{ add(){{}} }} }} }},
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
      const b = make({ pcWM: {} });                         // the shell, flag off
      out.shellFlagOff = b.api.enabled();
      const c = make({ pcWM: {} });
      c.w.localStorage.setItem('pc_os_toplevels','1');
      out.shellFlagOn = c.api.enabled();
    """)
    assert got["browser"] is False, "a browser tab would try to open an OS window"
    assert got["shellFlagOff"] is False, "stage 1 must stay off until it is turned on"
    assert got["shellFlagOn"] is True


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

def test_a_window_does_not_boot_a_second_client():
    """The point of sharing the opener's objects. Booting the client again per window would mint
    another relay pool, another subscription set and another signer for every window on screen."""
    boot = APP_JS[APP_JS.index("  async function boot(){"):]
    boot = boot[:boot.index("_bootCfg")]
    assert "PCOSWin.isWindow()" in boot
    assert "_bootAsWindow" in boot
    assert boot.index("PCOSWin.isWindow()") < boot.index("fetch('/client/config')") \
        if "fetch('/client/config')" in boot else True


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


def test_every_class_the_window_draws_is_styled():
    """The bug this repo keeps paying for: markup whose classes the stylesheet never defines renders
    as unstyled browser default. It shipped in the office save sheet earlier today."""
    body = APP_JS[APP_JS.index("async function _bootAsWindow"):]
    body = body[:body.index("async function boot(")]
    for cls in sorted(set(re.findall(r'class="([a-z][a-z0-9 _-]*)"', body))):
        for one in cls.split():
            if one in ("muted", "small"):
                continue
            assert f".{one}" in CSS, f"the window draws .{one}, which the stylesheet never defines"
    assert ".pc-oswin" in CSS
