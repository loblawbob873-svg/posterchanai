import re
import runpy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_native_snap_helper_is_shipped_and_bound_in_both_os_configs():
    helper = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-window-snap"
    assert helper.exists()
    code = helper.read_text()
    assert '"left", "right", "max", "edge"' in code
    assert '"move", "absolute", "position"' in code
    for cfg in (
        ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config",
        ROOT / "os/gentoo.sh",
    ):
        src = cfg.read_text()
        assert "$mod+Left  exec /usr/local/bin/pc-window-snap left" in src
        assert "$mod+Right exec /usr/local/bin/pc-window-snap right" in src
        assert "$mod+Up    exec /usr/local/bin/pc-window-snap max" in src


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


def test_super_arrow_routes_shell_focus_to_the_in_app_window(monkeypatch):
    """The compositor consumes Super+Right, so Chromium cannot be the only handler."""
    helper = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-window-snap"
    module = runpy.run_path(str(helper), run_name="pc_window_snap_test")
    sent = []
    monkeypatch.setattr(module["sys"], "argv", ["pc-window-snap", "right"])
    monkeypatch.setitem(module["main"].__globals__, "sway",
                        lambda *args: sent.append(args) or '{"nodes":[]}')
    monkeypatch.setitem(module["main"].__globals__, "focused",
                        lambda _tree: ({"id": 9, "app_id": "posterchan-desktop"}, {"rect": {}}))
    module["main"]()
    assert sent[-1] == ("-t", "send_tick", "pc:snap:right")


def test_shell_snap_tick_uses_the_focused_posterchan_window():
    src = (ROOT / "static/js/client/os.js").read_text()
    assert "/^pc:snap:(left|right|max)$/.test(p)" in src
    assert "wins.find(x=>x.el.classList.contains('focused'))" in src
    assert "snapTo(w, p.slice(8)==='max' ? 'max' : p.slice(8))" in src


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


def test_existing_identity_configs_are_migrated_to_native_snap():
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
    assert "pc-window-snap pc-key" in ebuild
    assert "focus output/d" in ebuild


def test_native_titlebars_use_the_posterchan_palette():
    for cfg in (
        ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config",
        ROOT / "os/gentoo.sh",
    ):
        src = cfg.read_text()
        assert "client.focused          #241438" in src
        assert "client.unfocused        #100d18" in src


def test_firefox_and_telegram_cannot_lose_the_native_snap_container():
    for cfg in (
        ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config",
        ROOT / "os/gentoo.sh",
    ):
        src = cfg.read_text()
        assert '[app_id="firefox"] floating enable, border normal 3' in src
        assert '[class="(?i)^firefox$"] floating enable, border normal 3' in src
        assert '[app_id="org.telegram.desktop"] floating enable, border normal 3' in src
        assert '[class="(?i)^(TelegramDesktop|telegram-desktop)$"] floating enable, border normal 3' in src


def test_existing_private_configs_gain_explicit_native_window_rules():
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
    assert 'native_rule in' in ebuild
    assert '[app_id="firefox"] floating enable, border normal 3' in ebuild
    assert '[app_id="org.telegram.desktop"] floating enable, border normal 3' in ebuild


def test_dragging_a_titlebar_to_an_output_edge_snaps_without_stealing_app_clicks():
    helper = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-window-snap").read_text()
    assert '"edge"' in helper
    assert 'side = "left"' in helper and 'side = "right"' in helper
    for cfg in (ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config",
                ROOT / "os/gentoo.sh"):
        src = cfg.read_text()
        assert "bindsym --border --release button1 exec /usr/local/bin/pc-window-snap edge" in src
        binding = src[src.index("pc-window-snap edge") - 100:src.index("pc-window-snap edge")]
        assert "--border" in binding
        assert "--whole-window" not in binding
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
    assert "pc-window-snap pc-key" in ebuild


def test_native_titlebar_corners_snap_to_output_quarters(monkeypatch):
    helper = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-window-snap"
    module = runpy.run_path(str(helper), run_name="pc_window_snap_corner_test")

    def run(rect):
        calls = []
        monkeypatch.setattr(module["sys"], "argv", ["pc-window-snap", "edge"])
        monkeypatch.setitem(module["main"].__globals__, "sway", lambda *_: '{"nodes":[]}')
        monkeypatch.setitem(module["main"].__globals__, "focused", lambda _tree:
                            ({"id": 7, "app_id": "firefox", "rect": rect},
                             {"rect": {"x": 1000, "y": 100, "width": 1200, "height": 900}}))
        monkeypatch.setattr(module["subprocess"], "check_call",
                            lambda argv, **_kw: calls.append(argv))
        module["main"]()
        return calls

    top_left = run({"x": 1000, "y": 100, "width": 500, "height": 400})
    assert top_left[-2][-6:] == ["width", "600", "px", "height", "414", "px"]
    assert top_left[-1][-3:] == ["position", "1000", "100"]

    bottom_right = run({"x": 1700, "y": 600, "width": 500, "height": 328})
    assert bottom_right[-2][-6:] == ["width", "600", "px", "height", "414", "px"]
    assert bottom_right[-1][-3:] == ["position", "1600", "514"]


def test_compositor_snap_api_supports_all_four_corner_zones():
    wm = (ROOT / "desktop/wm.js").read_text()
    snap = wm[wm.index("async snap(id, zone)"):wm.index("\n  move(", wm.index("async snap(id, zone)"))]
    assert "/^(top|bottom)-(left|right)$/.test(zone)" in snap
    assert "parts[0]==='bottom'" in snap and "parts[1]==='right'" in snap


def test_existing_identity_configs_receive_the_posterchan_native_chrome():
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
    for rule in ("titlebar_border_thickness 0", "titlebar_padding 8 6",
                 "client.focused #241438", "client.unfocused #100d18"):
        assert rule in ebuild


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


def test_the_runtime_palette_matches_the_shipped_config():
    """Two hand-maintained copies is how one of them ends up wrong — the recurring shape in this
    repo. Every client.* line the shell sends must be a line the config also has."""
    wm = (ROOT / "desktop/wm.js").read_text()
    block = wm[wm.index("static CHROME = ["):wm.index("];", wm.index("static CHROME = ["))]
    sent = [re.sub(r"\s+", " ", m.group(1)).strip()
            for m in re.finditer(r"'([^']+)'", block)]
    assert sent, "the CHROME list is empty — re-point this test"
    cfg = re.sub(r"\s+", " ", (ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config").read_text())
    for line in sent:
        if not line.startswith("client."):
            continue
        assert line in cfg, (
            f"the shell sends {line!r} at runtime and the shipped sway.config does not say it — "
            "a fresh install and an upgraded one would look different")


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
    assert "overlaps(it.rect, w.rect)" in plan, (
        "stashPlan no longer puts away a native app that a PosterChan window covers, so Telegram "
        "and Firefox sit on top of whatever you click"
    )
    assert "w.z > (it.z || 0)" in plan, (
        "the comparison lost its direction — a window BEHIND a native app would stash it, which is "
        "every window stashing everything it shares pixels with"
    )
    assert "pcWM.hide(it.native)" in src, "nothing carries the plan out"
