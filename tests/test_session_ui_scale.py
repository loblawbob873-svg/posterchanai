"""The PosterChanOS session scales the TOOLKITS, never the outputs.

THE ONE THING THAT MUST NOT HAPPEN HERE is the obvious fix. Setting the compositor's output scale to
1.25 makes every native app readable in a single line, and it was measured to cost every game
dearly: Xwayland is handed no `-scale`, so a fullscreen game is told to render 3072x2048 and Wayfire
upscales it to the 3840x2560 panel — blurry, and because the buffer never matches the output mode
DIRECT SCANOUT is impossible and every frame pays a full composite+scale pass. Output scale is the
one lever a game cannot opt out of.

So the session exports per-toolkit UI scaling instead, from pc-compositor-session — the parent of
Wayfire and therefore of everything the session goes on to start. These tests RUN the real script
(through its `PC_UI_SCALE_PROBE` seam and against a planted DRM tree), because the failure mode here
is a probe that answers confidently and wrongly, which no grep for a variable name can see.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SESSION = ROOT / "os/bin/pc-compositor-session"
PACKAGED = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-compositor-session"
GENTOO = ROOT / "os/gentoo.sh"

pytestmark = pytest.mark.skipif(not SESSION.exists(), reason="no PosterChanOS tree here")


def _probe(tmp_path, panels, **env):
    """Run the shipped script's scale probe against a fake /sys/class/drm."""
    drm = tmp_path / "drm"
    for i, (name, mode, status) in enumerate(panels):
        d = drm / f"card1-{name}"
        d.mkdir(parents=True)
        (d / "status").write_text(status + "\n")
        (d / "modes").write_text(mode + "\n")
    e = dict(os.environ)
    e.update({"PC_UI_SCALE_PROBE": "1", "PC_DRM_ROOT": str(drm),
              "HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "cfg")})
    for k in ("PC_UI_SCALE",):
        e.pop(k, None)
    e.update(env)
    done = subprocess.run(["sh", str(SESSION)], capture_output=True, text=True, timeout=60, env=e,
                          stdin=subprocess.DEVNULL)
    assert done.returncode == 0, done.stderr[-1500:]
    out = done.stdout.split()
    assert out and all("=" in kv for kv in out), (
        "the session script has no PC_UI_SCALE_PROBE seam, so what it decides about this machine's "
        "panels cannot be asked of it — by a test or by anyone debugging it. It printed: %r"
        % done.stdout[:400])
    return dict(kv.split("=", 1) for kv in out)


def test_the_script_is_valid_shell():
    done = subprocess.run(["sh", "-n", str(SESSION)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-1500:]


def test_a_4k_class_panel_is_scaled_and_an_ordinary_one_is_not(tmp_path):
    """The same script runs on 1080p laptops, so the factor is asked of the PANELS.

    A hardcoded 1.25 would make every laptop's text 25% too big, which is the mirror image of the
    bug being fixed and just as silent."""
    big = _probe(tmp_path / "a", [("DP-1", "3840x2560", "connected")])
    assert big == {"scale": "1.25", "dpi": "120", "cursor": "32"}, big

    small = _probe(tmp_path / "b", [("eDP-1", "1920x1080", "connected")])
    assert small == {"scale": "1", "dpi": "96", "cursor": "24"}, small


def test_a_wide_panel_is_not_a_high_resolution_one(tmp_path):
    """A 49" 5120x1440 is ~110ppi and must be left alone. Width alone cannot tell it from a 4K
    panel — every threshold that catches 3840 catches 5120 as well — so the probe reads HEIGHT
    too, exactly as the client stylesheet's tier does."""
    wide = _probe(tmp_path, [("DP-2", "5120x1440", "connected")])
    assert wide["scale"] == "1", wide


def test_a_disconnected_connector_is_not_a_panel(tmp_path):
    """Every machine has more DRM connectors than monitors, and a disconnected one still carries a
    `modes` file from whatever was last plugged in. Reading those would scale a laptop by the size
    of a television somebody once connected to it."""
    out = _probe(tmp_path, [("HDMI-A-1", "3840x2560", "disconnected"),
                            ("eDP-1", "1920x1080", "connected")])
    assert out["scale"] == "1", out


def test_one_big_panel_among_several_still_scales(tmp_path):
    """Two 3840x2560 monitors is the deployment this exists for, and a laptop docked to one 4K
    screen is the common case. The environment is per-SESSION — there is no way to give two
    monitors different toolkit scaling — so any 4K-class panel wins."""
    out = _probe(tmp_path, [("eDP-1", "1920x1080", "connected"),
                            ("DP-1", "3840x2560", "connected")])
    assert out["scale"] == "1.25", out


def test_an_explicit_choice_beats_the_probe(tmp_path):
    """The owner changed his mind about this number once already."""
    out = _probe(tmp_path / "a", [("eDP-1", "1920x1080", "connected")], PC_UI_SCALE="1.5")
    assert out == {"scale": "1.5", "dpi": "144", "cursor": "32"}, out

    cfg = tmp_path / "b-cfg/posterchanos"
    cfg.mkdir(parents=True)
    (cfg / "uiscale").write_text("2\n")
    out = _probe(tmp_path / "b", [("eDP-1", "1920x1080", "connected")],
                 XDG_CONFIG_HOME=str(tmp_path / "b-cfg"))
    assert out == {"scale": "2", "dpi": "192", "cursor": "48"}, out


def test_a_nonsense_override_falls_back_to_the_probe(tmp_path):
    """A truncated or hand-mangled config file must not turn into a scale of 0 — which awk would
    happily accept and which makes every font on the machine unreadable."""
    cfg = tmp_path / "cfg/posterchanos"
    cfg.mkdir(parents=True)
    (cfg / "uiscale").write_text("banana\n")
    out = _probe(tmp_path, [("DP-1", "3840x2560", "connected")],
                 XDG_CONFIG_HOME=str(tmp_path / "cfg"))
    assert out["scale"] == "1.25", out


def test_the_cursor_is_snapped_to_a_size_themes_actually_ship(tmp_path):
    """A cursor theme ships 24/32/48/64. Asking for 30 makes the compositor resample one, which is
    a soft cursor — the exact blur every other decision here avoids."""
    seen = {}
    for i, s in enumerate(("1", "1.25", "1.5", "1.75", "2", "2.5")):
        seen[s] = _probe(tmp_path / str(i), [("eDP-1", "1920x1080", "connected")],
                         PC_UI_SCALE=s)["cursor"]
    assert set(seen.values()) <= {"24", "32", "48", "64"}, seen
    assert seen["1"] == "24" and seen["2"] == "48", seen


SRC = SESSION.read_text(encoding="utf-8")


def test_the_toolkit_variables_are_exported_before_the_compositor_starts():
    """They have to be in the environment WAYFIRE inherits: everything the session starts is a child
    of it, and there is no way to push a new environment into a process that is already running."""
    for var in ("GDK_DPI_SCALE", "QT_FONT_DPI", "XCURSOR_SIZE", "XCURSOR_THEME"):
        assert re.search(r"export %s" % var, SRC), (
            "%s is never exported, so that toolkit renders at 1:1 on a 165ppi panel" % var)
        assert SRC.index("export %s" % var) < SRC.index("wayfire -c"), (
            "%s is exported after Wayfire is started, so nothing in the session inherits it" % var)


def test_the_surface_scaling_knobs_are_deliberately_not_used():
    """GDK_SCALE and QT_SCALE_FACTOR scale the whole SURFACE. GDK_SCALE is integer-only (its first
    step past 1 is 200%), and on Wayland both make clients submit an oversized buffer for the
    compositor to scale back down — which is the composite pass this whole change exists to avoid.
    Scaling text carries GTK/Qt chrome with it and leaves icons crisp at 1:1."""
    for var in ("GDK_SCALE", "QT_SCALE_FACTOR", "QT_SCREEN_SCALE_FACTORS"):
        assert not re.search(r"^\s*export .*%s=" % var, SRC, re.M), (
            "%s is exported: that is surface scaling, and it re-introduces the composite+scale pass "
            "the outputs were set back to 1 to avoid" % var)


def test_a_users_own_setting_still_wins():
    """These are session DEFAULTS. Somebody who exports their own in .bash_profile — which is
    exactly how this was first tried on the machine — must not have it overwritten."""
    for var in ("GDK_DPI_SCALE", "QT_FONT_DPI", "XCURSOR_SIZE"):
        assert re.search(r'\[ -n "\$\{%s:-\}" \] \|\| export %s=' % (var, var), SRC), (
            "%s is exported unconditionally, so a user's own value is discarded" % var)


def test_nothing_here_touches_the_output_scale():
    """Stated as a test because it is the fix everyone reaches for first, and it is the one that
    costs the games."""
    code = "\n".join(l for l in SRC.splitlines() if not l.lstrip().startswith("#"))
    assert "wlr-randr" not in code, (
        "the session is configuring outputs. Output scale is what makes Xwayland render a game at "
        "the wrong size and lose direct scanout; the whole point here is that it stays at 1.")


def test_xft_dpi_is_merged_without_racing_the_x_server():
    """Xft.dpi has NO environment variable — it lives in the X resource database, on a server that
    does not exist yet: Wayfire starts Xwayland itself, after this script's exports. Merging it
    inline would silently do nothing; waiting for it inline would hold the whole login behind an X
    server that on a machine with no X clients may never appear."""
    assert "xrdb -merge" in SRC, "nothing ever sets Xft.dpi, so pure-X11 clients stay at 96dpi"
    block = SRC[SRC.index("xsock="):SRC.index("xrdb -merge") + 200]
    assert "while [ ! -S" in block, "the merge does not wait for the Xwayland socket at all"
    assert re.search(r"\)\s*>/dev/null 2>&1 &", SRC), (
        "the Xft.dpi merge is not backgrounded — the login now waits on an X server")
    assert "command -v xrdb" in SRC, (
        "a machine without xrdb would log an error from the session script every boot")


def test_gtk4_is_scaled_through_the_settings_key_and_not_only_the_variable():
    """MEASURED ON THE MACHINE, GTK 4.20 against the live Wayland display:

        GDK_DPI_SCALE unset  -> gtk-xft-dpi 98304 (96dpi), "The quick brown fox"/Adwaita Sans 11 =
                                132x17px
        GDK_DPI_SCALE=1.25   -> 98304, 132x17px      (identical — no effect)
        GDK_DPI_SCALE=2      -> 98304, 132x17px      (identical — no effect)
        gtk-xft-dpi in gtk-4.0/settings.ini, with and without GTK_USE_PORTAL -> no effect
        org.gnome.desktop.interface text-scaling-factor 1.25
                             -> gtk-xft-dpi 122880 (120dpi), 165x21px

    GDK_DPI_SCALE is a GTK3 variable. Everything on this profile but Firefox is GTK4, so an
    env-only fix leaves the whole desktop at 96dpi while looking exactly like it worked — which is
    how it was first tried by hand in .bash_profile."""
    assert "text-scaling-factor" in SRC, (
        "only GDK_DPI_SCALE is set, and GTK4 ignores it — measured, no change at 1.25 or at 2")
    assert "gsettings set org.gnome.desktop.interface text-scaling-factor" in SRC
    assert "command -v gsettings" in SRC, "a machine without gsettings would error every login"


def test_the_gtk_setting_is_written_both_ways_and_never_over_a_users_own():
    """It PERSISTS, unlike an environment variable, so it has to be undone as well as done: a
    machine that goes back to an ordinary monitor would otherwise keep 125% text for ever with
    nothing on screen to say where it came from. And a value this script did not write is somebody's
    deliberate choice — the marker file is the only thing that can tell the two apart."""
    fn = SRC[SRC.index("pc_gtk_text_scale() {"):SRC.index("# Xft.dpi HAS NO ENVIRONMENT")]
    assert "uiscale-gtk" in fn, "nothing records what this script wrote, so it cannot undo it"
    assert '[ "$cur" = "1" ] || [ "$cur" = "$mark" ]' in fn, (
        "the setting is written unconditionally, so a hand-set text scale is clobbered every login")
    assert '[ "$cur" != "$pc_scale" ] || return 0' in fn, (
        "it rewrites the value it already holds, waking every GTK app on the desktop every login")
    # ...and it must be reachable when the probe says 1, which is the direction that undoes it.
    assert SRC.index("pc_gtk_text_scale\n") > SRC.index('note "ui scale'), \
        "the call moved"
    guard = SRC[SRC.index("(\n\tpc_gtk_text_scale"):]
    assert guard.index("pc_gtk_text_scale") < guard.index('[ "$pc_dpi" -ne 96 ]'), (
        "the GTK setting is only touched when scaling UP, so going back to a small screen leaves "
        "the desktop permanently at the big one's text size")


def test_gsettings_schemas_are_installed_by_the_profile():
    """`gsettings get org.gnome.desktop.interface …` on a machine without the schema fails, and the
    guard then skips silently — GTK4 stays at 96dpi with nothing to say why."""
    pkgs = re.search(r'POSTERCHANOS_PACKAGES="(.*?)"', GENTOO.read_text(encoding="utf-8"), re.S)
    assert "gnome-base/gsettings-desktop-schemas" in pkgs.group(1).replace("\\\n", " ").split()


def test_xrdb_is_installed_by_the_profile():
    """A tool with no package is a control that silently does nothing on a fresh build."""
    pkgs = re.search(r'POSTERCHANOS_PACKAGES="(.*?)"', GENTOO.read_text(encoding="utf-8"), re.S)
    assert pkgs, "the profile's package list moved"
    assert "x11-apps/xrdb" in pkgs.group(1).replace("\\\n", " ").split()


def test_the_installed_copy_is_the_same_file():
    """os/bin is the source and files/ is what Portage installs. They are separate files, so an edit
    to one is an edit that never reaches a machine."""
    assert SESSION.read_bytes() == PACKAGED.read_bytes(), (
        "os/bin/pc-compositor-session and the packaged copy have diverged")


def _gtk_fn() -> str:
    i = SRC.index("pc_gtk_text_scale() {")
    depth, j = 0, SRC.index("{", i)
    for k in range(j, len(SRC)):
        if SRC[k] == "{":
            depth += 1
        elif SRC[k] == "}":
            depth -= 1
            if depth == 0:
                return SRC[i:k + 1]
    raise AssertionError("pc_gtk_text_scale")


def _run_gtk(tmp_path, current, scale="1.25", marker=None):
    """RUN the shipped function against a stub gsettings, and report what it did.

    Extracted rather than driven through the whole script because the real one ends in `exec
    wayfire`. The stub records every `set` and answers `get` from a file, so a second `set` in one
    run — the thing that would wake every GTK app on the desktop — is visible."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    value = tmp_path / "value"
    value.write_text(current + "\n")
    (bin_dir / "gsettings").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = get ]; then cat %s; else printf "%%s\\n" "$4" > %s; '
        'printf "%%s\\n" "$4" >> %s; fi\n' % (value, value, tmp_path / "sets"))
    (bin_dir / "gsettings").chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    if marker is not None:
        (state / "uiscale-gtk").write_text(marker + "\n")
    (tmp_path / "bus").write_text("")   # not a socket: the fallback branch must not be taken
    prog = (
        'state_dir=%s\npc_scale=%s\nnote(){ printf "%%s\\n" "$*" >> %s; }\n'
        'DBUS_SESSION_BUS_ADDRESS=unix:path=/dev/null\nexport DBUS_SESSION_BUS_ADDRESS\n'
        % (state, scale, tmp_path / "log")) + _gtk_fn() + "\npc_gtk_text_scale\n"
    e = dict(os.environ)
    e["PATH"] = "%s:%s" % (bin_dir, e["PATH"])
    done = subprocess.run(["sh", "-c", prog], capture_output=True, text=True, timeout=60, env=e)
    assert done.returncode == 0, done.stderr[-1500:]
    sets = (tmp_path / "sets").read_text().split() if (tmp_path / "sets").exists() else []
    mark = (state / "uiscale-gtk").read_text().strip() if (state / "uiscale-gtk").exists() else None
    return {"sets": sets, "value": value.read_text().strip(), "marker": mark,
            "log": (tmp_path / "log").read_text() if (tmp_path / "log").exists() else ""}


def test_the_gtk_setting_is_actually_written_when_it_is_at_the_default(tmp_path):
    out = _run_gtk(tmp_path, "1.0")
    assert out["sets"] == ["1.25"], out
    assert out["marker"] == "1.25", out


def test_it_is_not_rewritten_when_it_already_agrees(tmp_path):
    """Writing it again is not free: every GTK app on the desktop re-reads and re-lays out."""
    out = _run_gtk(tmp_path, "1.25")
    assert out["sets"] == [], out


def test_it_is_put_back_when_the_screen_is_ordinary_again(tmp_path):
    """The direction an environment variable gets for free and a stored setting does not."""
    out = _run_gtk(tmp_path, "1.25", scale="1", marker="1.25")
    assert out["sets"] == ["1"], out
    assert out["value"] == "1", out


def test_a_value_somebody_set_by_hand_is_left_alone(tmp_path):
    out = _run_gtk(tmp_path, "1.5")
    assert out["sets"] == [], out
    assert "not ours" in out["log"], out


def test_a_missing_gsettings_is_not_an_error(tmp_path):
    """It is guarded, so a build without the schemas degrades to "GTK4 stays at 96dpi", not to an
    error printed into the session log on every login."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    prog = ('state_dir=%s\npc_scale=1.25\nnote(){ :; }\nPATH=%s\nexport PATH\n'
            'DBUS_SESSION_BUS_ADDRESS=unix:path=/dev/null\nexport DBUS_SESSION_BUS_ADDRESS\n'
            % (state, bin_dir)) + _gtk_fn() + "\npc_gtk_text_scale\necho survived\n"
    done = subprocess.run(["sh", "-c", prog], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and "survived" in done.stdout, done.stderr[-800:]
