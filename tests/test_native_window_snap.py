import builtins
import re
import runpy
from pathlib import Path

from tests.wayfire_config import CONFIG, bindings, sections

ROOT = Path(__file__).resolve().parents[1]
SNAP = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-window-snap"


def test_native_snap_helper_is_shipped_and_bound_in_both_os_configs():
    helper = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-window-snap"
    assert helper.exists()
    code = helper.read_text()
    assert '"left", "right", "max", "edge"' in code
    assert '"move-left": "left"' in code
    assert '"window-rules/configure-view"' in code, "nothing applies the snapped geometry"
    binds = bindings()
    for arrow, action in (("KEY_LEFT", "pc-window-snap left"), ("KEY_RIGHT", "pc-window-snap right"),
                          ("KEY_UP", "pc-window-snap max")):
        chord = "<super> " + arrow
        assert chord in binds, f"{chord} is unbound"
        assert action in binds[chord], f"{chord} runs {binds[chord]!r}, not {action}"


def drive(win, side, outputs=None):
    """Run the helper for one focused view; return `(ipc_calls, ticks)`.

    Only the IPC round trip and the shell tick are stubbed, so the real classification, the real
    branch ordering and the real geometry arithmetic all execute.
    """
    module = runpy.run_path(str(SNAP), run_name="pc_window_snap_drive")
    ipc, ticks = [], []
    boxes = outputs or [{"id": 1, "name": "DP-1",
                         "geometry": {"x": 0, "y": 0, "width": 1920, "height": 1080},
                         "workarea": {"x": 0, "y": 0, "width": 1920, "height": 1080}}]

    def fake_wayfire(method, data=None):
        if method == "window-rules/list-views":
            return [dict(win, activated=True, mapped=True)]
        if method == "window-rules/list-outputs":
            return boxes
        ipc.append((method, data))
        return {}

    module["wayfire_main"].__globals__["wayfire"] = fake_wayfire
    module["wayfire_main"].__globals__["shell_action"] = ticks.append
    module["wayfire_main"](side)
    return ipc, ticks


def test_native_snap_never_resizes_a_posterchan_shell_surface(monkeypatch):
    """Super+Right must not halve an Electron shell and force its web UI into Classic mode.

    app_id is deliberately absent: that is the real Wayland metadata race that made the old exact
    string comparison ineffective.  A renderer process is identified through its main-process
    ancestry instead.
    """
    helper = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-window-snap"
    module = runpy.run_path(str(helper), run_name="pc_window_snap_test")
    monkeypatch.setitem(module["process_chain"].__globals__, "process_chain",
                        lambda _pid: iter(("/opt/posterchan/posterchan-desktop --shell",)))
    assert module["is_posterchan_shell"]({"app_id": None, "pid": 4242}) is True


def test_electron_44_reverse_dns_shell_identity_needs_no_pid_fallback():
    helper = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-window-snap"
    module = runpy.run_path(str(helper), run_name="pc_window_snap_identity_test")
    module["is_posterchan_shell"].__globals__["process_chain"] = lambda _pid: (_ for _ in ()).throw(
        AssertionError("stable app_id must not need process ancestry"))
    assert module["is_posterchan_shell"]({"app_id": "place.poster.desktop", "pid": 0}) is True


def test_process_ancestry_survives_spaces_and_parentheses_in_proc_comm(monkeypatch):
    """A renderer name must not corrupt ppid parsing and expose its whole shell to native snap."""
    helper = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-window-snap"
    module = runpy.run_path(str(helper), run_name="pc_window_snap_proc_stat_test")
    opened = {
        "/proc/4242/cmdline": b"/usr/lib/electron/electron\0--type=renderer\0",
        "/proc/4242/stat": "4242 (Web Content) X11) S 3131 1 1 0 0\n",
        "/proc/3131/cmdline": b"/opt/posterchan/posterchan-desktop\0--shell\0",
        "/proc/3131/stat": "3131 (posterchan-desktop) S 1 1 1 0 0\n",
        "/proc/1/cmdline": b"/sbin/init\0",
        "/proc/1/stat": "1 (init) S 0 1 1 0 0\n",
    }

    class FakeFile:
        def __init__(self, value):
            self.value = value
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self):
            return self.value

    monkeypatch.setattr(builtins, "open",
                        lambda path, mode="r", **_kw: FakeFile(opened[path]))
    assert module["is_posterchan_shell"]({"app_id": "", "pid": 4242}) is True



def test_repeated_super_arrows_route_to_the_internal_window_without_resizing_shell():
    """Super+Arrow on the desktop surface must reach the RENDERER, never resize the surface itself:
    halving it makes the viewport narrow enough that the web desktop falls back to Classic."""
    shell = {"id": 31, "app-id": "place.poster.desktop", "title": "PosterChan · Nostr", "output-id": 1}
    ticks = []
    for side in ("right", "right", "left", "right", "max", "left"):
        ipc, sent = drive(shell, side)
        assert ipc == [], "Super+Arrow resized the per-monitor shell instead of its internal window"
        ticks += sent
    assert ticks == ["pc:snap:right", "pc:snap:right", "pc:snap:left",
                     "pc:snap:right", "pc:snap:max", "pc:snap:left"]




def test_cross_monitor_shortcut_moves_the_in_app_window_not_the_shell():
    shell = {"id": 32, "app-id": "place.poster.desktop", "title": "PosterChan · Nostr", "output-id": 1}
    ipc, ticks = drive(shell, "move-right")
    assert ticks[-1] == "pc:move-output:right"
    assert ipc == [], "the entire per-output shell was moved and its source monitor went black"




def test_cross_monitor_shortcut_still_moves_a_native_app():
    firefox = {"id": 91, "app-id": "firefox", "title": "Mozilla Firefox", "output-id": 1}
    ipc, ticks = drive(firefox, "move-left")
    assert ticks[-1] == "pc:move-native:91:left"
    assert ipc == [], "native shortcut bypassed the state-preserving renderer handoff"



def test_shell_move_tick_uses_state_preserving_monitor_handoff():
    src = (ROOT / "static/js/client/os.js").read_text()
    assert "/^pc:move-output:(left|right|up|down)$/.test(p)" in src
    assert "moveWindowToMonitor(w,p.slice(15))" in src
    move = src[src.index("async function moveWindowToMonitor"):
               src.index("function startDrag", src.index("async function moveWindowToMonitor"))]
    assert "pcWM.handoff(w.native,direction,nativeHandoffPlacement(w,direction))" in move
    assert "sendFrameHandoff(w,direction,0,false)" in move
    assert "/^pc:move-native:\\d+:(left|right|up|down)$/.test(p)" in src
    assert "nativeWins().find(x=>Number(x.native)===id)" in src



def test_mouse_edge_release_cannot_snap_the_reverse_dns_shell_surface():
    """`edge` is what a titlebar drag lands on. It must be refused for the desktop surface under
    every app-id spelling Electron has used, or a drag near a screen edge halves the desktop."""
    for app_id in ("place.poster.desktop", "PosterChan", "posterchan-desktop"):
        shell = {"id": 5, "app-id": app_id, "title": "PosterChan · Nostr", "output-id": 1}
        ipc, ticks = drive(shell, "edge")
        assert ipc == [], f"{app_id}: a mouse-edge release resized the desktop surface"



def test_super_arrow_routes_shell_focus_to_the_in_app_window():
    shell = {"id": 5, "app-id": "place.poster.desktop", "title": "PosterChan · Nostr", "output-id": 1}
    ipc, ticks = drive(shell, "left")
    assert ticks == ["pc:snap:left"]
    assert ipc == []



def test_shell_snap_tick_uses_the_focused_posterchan_window():
    src = (ROOT / "static/js/client/os.js").read_text()
    assert "/^pc:snap:(left|right|max)$/.test(p)" in src
    assert "wins.find(x=>x.el.classList.contains('focused'))" in src
    assert "snapTo(w, p.slice(8)==='max' ? 'max' : p.slice(8))" in src


def test_super_snap_and_monitor_move_suppress_the_trailing_start_release():
    """Sway emits the command tick before its independent Super_L release binding emits Start."""
    src = (ROOT / "static/js/client/os.js").read_text()
    tick = src[src.index("const p = String(ev.payload || '')"):
               src.index("if(p.startsWith('pc:update-installed:'))")]
    assert "/^pc:(?:snap:|move-output:|move-native:)/.test(p)" in tick
    assert "_suppressStartUntil=Date.now()+1200" in tick
    assert "toggleStart(false)" in tick
    start = src[src.index("if(p === 'pc:start')"):][:220]
    assert "Date.now() < _suppressStartUntil" in start
    assert "toggleStart(false)" in start


def test_renderer_first_subscription_cannot_drop_all_super_shortcuts():
    """The renderer and recovery setup race; either one must subscribe the socket to tick."""
    main = (ROOT / "desktop/main.js").read_text()
    assert "const NAMES = ['window', 'workspace', 'output', 'tick']" in main
    assert "await wm().subscribe(['window','workspace','output','tick'])" in main


def test_native_snap_still_accepts_real_native_apps():
    helper = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-window-snap"
    module = runpy.run_path(str(helper), run_name="pc_window_snap_test")
    module["is_posterchan_shell"].__globals__["process_chain"] = lambda _pid: iter(("firefox",))
    assert module["is_posterchan_shell"]({"app_id": "firefox", "pid": 4243}) is False



def test_the_snap_helper_is_installed_by_the_package():
    """It is bound by absolute path, so an unshipped helper is a chord that does nothing at all.

    This replaces a check that the per-account Sway configs were migrated to gain these bindings.
    There are no per-account compositor configs any more: one package-owned /etc/wayfire.ini carries
    them, so an upgrade cannot leave an account behind.
    """
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
    assert " pc-window-snap " in ebuild
    assert 'doins "${FILESDIR}/wayfire.ini"' in ebuild



def test_native_titlebars_use_the_posterchan_palette():
    """Sway had five colours per state; Wayfire's decoration plugin has two. The one that matters is
    the focused titlebar, which is what somebody sees next to PosterChan's own chrome."""
    decoration = sections()["decoration"]
    assert decoration["active_color"].lower().lstrip("\\").startswith("#241438")
    assert decoration["inactive_color"].lower().lstrip("\\").startswith("#171222")


def test_firefox_and_telegram_cannot_lose_the_native_snap_container():
    """SWAY NEEDED A RULE PER APPLICATION; WAYFIRE NEEDS AN ABSENCE.

    Sway tiled by default, so Firefox and Telegram each needed `floating enable, border normal 3` or
    they were tiled into the shell's layout with no frame. Wayfire floats and decorates every
    toplevel, so what has to be true is the opposite: they must NOT be named in the list of surfaces
    excluded from server-side decoration, which exists for PosterChan's own chrome. A rule per app
    would be the bug now -- an entry that matched Firefox would take its frame away.
    """
    ignore = sections()["decoration"]["ignore_views"].lower()
    for app in ("firefox", "telegram", "org.telegram.desktop"):
        assert app not in ignore, f"{app} is excluded from decoration and would come up frameless"
    assert int(sections()["decoration"]["border_size"]) == 3
    assert "preferred_decoration_mode = server" in CONFIG.read_text(encoding="utf-8")


def test_dragging_a_titlebar_to_an_output_edge_snaps_without_stealing_app_clicks():
    helper = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-window-snap").read_text()
    assert '"edge"' in helper
    assert 'side = "left"' in helper and 'side = "right"' in helper
    # Sway needed a `--border --release button1` binding to notice a titlebar drag reaching an
    # edge, and `--border` rather than `--whole-window` so the press was not stolen from the app.
    # Wayfire's move plugin does the edge detection itself, which is both fewer moving parts and the
    # reason the binding is gone: `enable_snap` IS this feature.
    move = sections()["move"]
    assert move["enable_snap"] == "true", "dragging a window to an edge no longer snaps it"
    assert int(move["snap_threshold"]) > 0
    assert int(move["quarter_snap_threshold"]) > 0, "corner drags no longer make quarters"
    assert "BTN_LEFT" in move["activate"], "there is no drag gesture to snap with"
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
    # pc-window-close sits between them in the install loop; match each helper on its own so
    # adding a third never reads as one going missing.
    for helper in ("pc-window-snap", "pc-key"):
        assert f" {helper} " in ebuild, f"{helper} is not in the ebuild install loop"



def test_native_titlebar_corners_snap_to_output_quarters():
    """A window already in a corner gets a quarter, not a half — the geometry, not just the branch."""
    firefox = {"id": 91, "app-id": "firefox", "title": "Mozilla Firefox", "output-id": 1,
               "geometry": {"x": 0, "y": 0, "width": 400, "height": 300}}
    ipc, _ = drive(firefox, "edge")
    assert ipc, "a corner drag snapped nothing"
    method, data = ipc[-1]
    assert method == "window-rules/configure-view"
    box = data["geometry"]
    assert box["width"] == 960, box
    assert box["height"] == (1080 - 72) // 2, box




def test_native_right_snap_atomically_fills_offset_outputs_usable_height():
    """On a second, offset monitor the right half must fill THAT output — and the geometry is
    output-LOCAL, so an absolute x would put the window a whole monitor away."""
    outputs = [{"id": 3, "name": "DP-2",
                "geometry": {"x": 1920, "y": 0, "width": 2560, "height": 1440},
                "workarea": {"x": 0, "y": 0, "width": 2560, "height": 1440}}]
    firefox = {"id": 91, "app-id": "firefox", "title": "Mozilla Firefox", "output-id": 3}
    ipc, _ = drive(firefox, "right", outputs=outputs)
    method, data = ipc[-1]
    assert method == "window-rules/configure-view"
    assert data["geometry"] == {"x": 1280, "y": 0, "width": 1280, "height": 1440 - 72}, data



def test_compositor_snap_api_supports_all_four_corner_zones():
    wm = (ROOT / "desktop/wm.js").read_text()
    snap = wm[wm.index("async snap(id, zone)"):wm.index("\n  move(", wm.index("async snap(id, zone)"))]
    assert "/^(top|bottom)-(left|right)$/.test(zone)" in snap
    assert "parts[0]==='bottom'" in snap and "parts[1]==='right'" in snap



def test_the_native_chrome_is_package_owned_rather_than_migrated():
    """Same reason as above: the palette lives in the shipped config, not in a copy per account."""
    decoration = sections()["decoration"]
    assert decoration["active_color"].lower().lstrip("\\").startswith("#241438")
    assert int(decoration["border_size"]) == 3



def test_the_native_palette_is_applied_at_RUNTIME_not_only_from_a_config_file():
    """Firefox and Telegram kept sway's own colours for days after the palette was "fixed".

    The rules were only ever in sway.config (and os/gentoo.sh's copy of it). A config file is read
    once, when the session starts, and portage does not silently replace an existing one on upgrade
    — that is what etc-update is for. So every fix reached a freshly provisioned machine and no
    other, which is exactly what "still missing the same window decorations, many days" describes.

    swaymsg applies them to the RUNNING compositor, so an installed machine gets them on the next
    shell start rather than the next reinstall."""
    wm = (ROOT / "desktop/wm.js").read_text()
    assert "async applyChrome()" in wm, "nothing applies the window chrome at runtime"
    main = (ROOT / "desktop/main.js").read_text()
    assert "applyChrome()" in main, "applyChrome exists but nothing ever calls it"


def test_every_adopted_native_window_loses_stale_sticky_state_by_identity():
    """A restored Firefox private/Telegram window must not follow every workspace or output."""
    main = (ROOT / "desktop/main.js").read_text()
    decorate = main[main.index("async function decorateNative"):
                    main.index("ipcMain.handle('pc:display:status'")]
    # STICKY IS THE POINT OF THIS ASSERTION, not the border. The border now follows whether the
    # shell HOSTS the window: hosted, the PosterChan frame is the only chrome; unhosted, sway must
    # draw its own or Firefox has no decoration at all ("cant maximize and minimize"). Sticky is
    # cleared either way, which is what this test is actually about.
    assert "wm().decorate(Number(id),!!hosted)" in decorate
    sway = (ROOT / "desktop/wm.js").read_text()
    assert "(hosted?'border none':'border normal 3')+', sticky disable'" in sway
    assert "hosted?'border none':'border normal 3'" in sway, (
        "the Sway rollback backend must still decorate an unhosted native window")
    assert "fullscreen disable" not in decorate, "adoption must preserve app-requested fullscreen"


def test_native_decoration_is_reasserted_before_focus_and_monitor_handoff_focus():
    """Sticky state can change after adoption; focus and transfer must repair it every time."""
    os_src = (ROOT / "static/js/client/os.js").read_text()
    focus = os_src[os_src.index("function _focusNativeDecorated"):
                   os_src.index("function _focusNativeWhenShown")]
    assert "pcWM.decorate(id)" in focus and "_focusCompositorCurrent(id,lease)" in focus
    assert focus.index("pcWM.decorate(id)") < focus.index("_focusCompositorCurrent(id,lease)")
    main = (ROOT / "desktop/main.js").read_text()
    handoff = main[main.index("ipcMain.handle('pc:wm:handoff'"):
                   main.index("ipcMain.handle('pc:wm:handoff-frame'")]
    assert "await decorateNative(nativeId)" in handoff
    assert handoff.index("await decorateNative(nativeId)") < handoff.index("await wm().focus(nativeId)")



def test_there_is_only_one_copy_of_the_native_palette():
    """TWO HAND-MAINTAINED COPIES IS HOW ONE OF THEM ENDS UP WRONG, and this had exactly two.

    The Sway backend pushed a `client.*` palette over IPC at runtime (`WM.CHROME`) while the shipped
    config declared the same colours, so a fresh install and an upgraded one could disagree about
    what a native titlebar looked like. Wayfire's backend deliberately pushes NOTHING -- the config
    is the only source -- so the check is no longer "the two agree", it is "there is still only one".
    """
    wayfire_backend = (ROOT / "desktop/wm-wayfire.js").read_text(encoding="utf-8")
    body = wayfire_backend.split("applyChrome(", 1)[1].split("\n", 1)[0]
    assert "Promise.resolve" in body, (
        "the Wayfire backend has started pushing chrome at runtime; that is a second copy of the "
        "palette in wayfire.ini and they will drift")
    assert "client.focused" not in wayfire_backend
    # And the one remaining copy is the shipped config.
    assert sections()["decoration"]["active_color"].lower().lstrip("\\").startswith("#241438")



def test_focusing_a_posterchan_window_parks_compositor_windows_above_it():
    """Clicking a PosterChan window puts it in front of Telegram/Firefox.

    It cannot be done by stacking: a native app is a FLOATING sway window and this desktop is the
    one TILED window, and sway paints floating above tiled. So the app covering the window you
    clicked has to leave the screen, and the decision of WHICH apps those are is `stashPlan` — it
    is not made in `focusWin`, which only assigns the new z-order that stashPlan then reads.

    This test used to read `focusWin`'s first 6000 characters for `pcWM.hide`, which is wrong twice
    over: the parking moved to the placement pass, and a fixed slice reports a function that merely
    GREW as a function that lost its behaviour. Assert the contract where it actually lives."""
    src = (ROOT / "static/js/client/os.js").read_text()
    start = src.index("function focusWin(w, render)")
    focus = src[start:src.index("\n  function ", start + 10)]
    # Focus assigns the stacking order the plan is computed from, and asks for a re-plan.
    assert "nextZ()" in focus
    assert "nsync()" in focus

    nat = (ROOT / "static/js/client/osnative.js").read_text()
    plan = nat[nat.index("function stashPlan"):]
    plan = plan[:plan.index("\n  }")]
    assert "coversMoreThanASliver(it.rect, w.rect)" in plan, (
        "stashPlan no longer puts away a native app that a PosterChan window covers, so Telegram "
        "and Firefox sit on top of whatever you click"
    )
    # It is a threshold, not a plain intersection: one shared pixel used to park a whole app.
    # tests/test_native_sliver_overlap.py drives that rule against real measured geometry.
    assert "function coversMoreThanASliver" in nat
    assert "w.z > (it.z || 0)" in plan, (
        "the comparison lost its direction — a window BEHIND a native app would stash it, which is "
        "every window stashing everything it shares pixels with"
    )
    assert "pcWM.hide(it.native)" in src, "nothing carries the plan out"
