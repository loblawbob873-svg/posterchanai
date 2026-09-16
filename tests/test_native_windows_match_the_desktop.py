"""A NATIVE WINDOW ON POSTERCHANOS WEARS THE SAME FRAME AS A POSTERCHAN WINDOW.

Reported as "Firefox, foot, maybe others, window color has a different color from the rest of the
desktop and no border around window like we do to mimic hyprland's border". Measured in a headless
Wayfire with the shipped config before this change:

  * foot (server-decorated): title bar AND border one teal, #257281 -- 29,266 pixels of it, matching
    neither the desktop nor a PosterChan window, and no bright edge anywhere;
  * a client-decorated window (Firefox, GTK; stood in for by `foot -o csd.preferred=client`): no
    compositor frame at all, because Wayfire's decoration only frames clients that ask for it and
    Firefox's libxul has no zxdg_decoration_manager_v1;
  * foot's content: #242424 on a #0a0a0f desktop.

The frame is now two jobs done by two things: [decoration] paints the title bar the client's raised
surface, and the posterchan-shell plugin draws an accent ring around every application window --
decorated or not -- in the colours `#pc-oswin-frame` uses. The last test here runs that for real
when the machine has Wayfire, foot and grim.
"""
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INI = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/wayfire.ini").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
PLUGIN_DIR = ROOT / "os/overlay/gui-libs/posterchan-wayfire-shell"
CPP = (PLUGIN_DIR / "files/posterchan-shell.cpp").read_text(encoding="utf-8")
XML = (PLUGIN_DIR / "files/posterchan-shell.xml").read_text(encoding="utf-8")
FOOT = ROOT / "os/overlay/app-misc/posterchanos-shell/files/foot"
SHELL_EBUILD = next((ROOT / "os/overlay/app-misc/posterchanos-shell").glob("posterchanos-shell-*.ebuild"))


def _root_token(name):
    root = CSS[CSS.index(":root{"):]
    root = root[:root.index("}")]
    m = re.search(re.escape("--" + name) + r"\s*:\s*([^;]+);", root)
    assert m, f"--{name} is not in :root"
    return m.group(1).strip().lower()


def _hex(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _section(name):
    body = re.split(r"(?m)^\[" + re.escape(name) + r"\]\s*$", INI, maxsplit=1)[1]
    return re.split(r"(?m)^\[", body, maxsplit=1)[0]


def _option(section, key, raw=False):
    m = re.search(r"(?m)^" + re.escape(key) + r"\s*=\s*(.+?)\s*$", _section(section))
    assert m, f"{key} is not set in [{section}]"
    return m.group(1) if raw else m.group(1).lstrip("\\").lower()


def _frame_alpha(selector):
    m = re.search(re.escape(selector) + r"\{[^}]*?rgba\(var\(--accent-rgb\),\s*([.\d]+)\)", CSS, re.S)
    assert m, f"{selector} no longer draws the accent at an opacity"
    return float(m.group(1))


def _blend(alpha):
    accent = [int(c) for c in _root_token("accent-rgb").split(",")]
    bg = _hex(_root_token("bg"))
    return "#%02x%02x%02xff" % tuple(round(a * alpha + b * (1 - alpha)) for a, b in zip(accent, bg))


# ----------------------------------------------------------------------------------- the ring's values

def test_the_ring_is_the_posterchan_frame_colour_over_the_desktop():
    assert _option("posterchan-shell", "window_border") == "true"
    assert _option("posterchan-shell", "window_border_active_color") == _blend(_frame_alpha("#pc-oswin-frame.focused"))
    assert _option("posterchan-shell", "window_border_inactive_color") == _blend(_frame_alpha("#pc-oswin-frame"))


def test_the_ring_the_band_and_the_posterchan_frame_are_one_width():
    """Equal to [decoration] border_size, or on a decorated window the ring either leaves a strip of
    title-bar colour at the edge or covers a strip of the window's content."""
    client_px = int(re.search(r"#pc-oswin-frame\{[^}]*?border:(\d+)px", CSS, re.S).group(1))
    assert int(_option("posterchan-shell", "window_border_size")) == client_px
    assert int(_option("decoration", "border_size")) == client_px


def test_the_title_bar_is_a_surface_not_the_accent():
    """Wayfire paints title bar and border band one colour; the accent went to the ring."""
    assert _option("decoration", "active_color") == _root_token("bg2") + "ff"
    assert _option("decoration", "inactive_color") == _root_token("bg") + "ff"
    assert _option("decoration", "font_color") == _root_token("text") + "ff"


def test_the_metadata_defaults_are_the_shipped_values():
    """/etc/wayfire.ini is portage-owned and may lag a package update (a pending ._cfg file); the
    .xml ships with the .so, so a machine on an older ini must still draw the same ring."""
    for key in ("window_border", "window_border_size", "window_border_active_color",
                "window_border_inactive_color"):
        m = re.search(r'<option name="' + key + r'"[^>]*>.*?<default>([^<]+)</default>', XML, re.S)
        assert m, f"{key} is not declared in posterchan-shell.xml -- an undeclared option segfaults Wayfire"
        assert m.group(1).strip().lower() == _option("posterchan-shell", key), key


# ------------------------------------------------------------------------------- the plugin's rules

def test_every_surface_decoration_skips_is_one_the_ring_skips():
    """PosterChan's own windows draw #pc-oswin-frame; a compositor ring on top would be two borders."""
    skipped = re.findall(r'app_id is "([^"]+)"', _option("decoration", "ignore_views", raw=True))
    assert skipped
    rule = CPP[CPP.index("static bool is_posterchan"):]
    rule = rule[:rule.index("\n}")]
    for app in skipped:
        assert f'"{app}"' in rule, f"the ring would frame {app}, which draws its own frame"
    assert '"PosterChan Desktop"' in rule and '"PosterChan Window"' in rule


def test_the_ring_is_not_drawn_on_a_fullscreen_window():
    outer = CPP[CPP.index("wf::geometry_t outer()"):]
    outer = outer[:outer.index("\n    }")]
    assert "st.fullscreen" in outer


def test_a_client_decorated_window_is_framed_at_its_geometry_not_its_surface():
    """Measured: at the surface origin the ring framed foot's text area and left its title bar out."""
    assert "wlr_xdg_surface_try_from_wlr_surface" in CPP and "xdg->geometry.x" in CPP
    assert "__has_include(<xdg-shell-protocol.h>)" in CPP and "#error" in CPP
    ebuild = next(PLUGIN_DIR.glob("posterchan-wayfire-shell-*.ebuild")).read_text()
    assert "stable/xdg-shell/xdg-shell.xml" in ebuild and "xdg-shell-protocol.h" in ebuild


def test_the_ring_is_above_the_decoration():
    """Measured: added behind it, the decoration's band covered the ring on every decorated window."""
    assert "wf::scene::add_front(view->get_surface_root_node(), entry->node)" in CPP
    assert "add_back(view->get_surface_root_node(), entry->node)" not in CPP


def test_the_ring_options_are_looked_up_never_wrapped():
    for key in ("window_border", "window_border_size", "window_border_active_color",
                "window_border_inactive_color"):
        assert f'get_option<' in CPP and f'"posterchan-shell/{key}"' in CPP
        assert not re.search(r'option_wrapper_t<[^>]+>\s*\w+\{"posterchan-shell/' + key, CPP)


def test_the_new_plugin_actually_reaches_installed_machines():
    """A .so change under an unchanged revision is never re-emerged."""
    ebuild = next(PLUGIN_DIR.glob("posterchan-wayfire-shell-*.ebuild"))
    version = re.match(r"posterchan-wayfire-shell-(.+)\.ebuild", ebuild.name).group(1)
    assert f">=gui-libs/posterchan-wayfire-shell-{version}" in SHELL_EBUILD.read_text()
    assert version != "1.0.1-r4", "the border shipped under the revision that predates it"


# ------------------------------------------------------------------------------------ app colours

def _run_foot_wrapper(tmp, with_config):
    stub = tmp / "foot-stub"
    stub.write_text('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done\n')
    stub.chmod(0o755)
    wrapper = tmp / "foot"
    wrapper.write_text(FOOT.read_text().replace("/usr/bin/foot", str(stub)))
    wrapper.chmod(0o755)
    home = tmp / "home"
    (home / ".config/foot").mkdir(parents=True, exist_ok=True)
    if with_config:
        (home / ".config/foot/foot.ini").write_text("[colors]\nbackground=330000\n")
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    out = subprocess.run([str(wrapper), "-o", "colors.background=user", "-T", "mine"], env=env,
                         capture_output=True, text=True, check=True)
    return out.stdout.splitlines()


def test_foot_wears_the_desktop_colours_unless_the_person_chose_their_own(tmp_path):
    args = _run_foot_wrapper(tmp_path, with_config=False)
    assert f"colors.background={_root_token('bg').lstrip('#')}" in args
    assert f"colors.foreground={_root_token('text').lstrip('#')}" in args
    assert args[-4:] == ["-o", "colors.background=user", "-T", "mine"], "the person's own arguments must come last"
    args = _run_foot_wrapper(tmp_path, with_config=True)
    assert not any(a.startswith("colors.") and a != "colors.background=user" for a in args), (
        "an -o beats foot.ini, so passing colours over a person's own config overrides it")


def test_firefox_defaults_to_its_dark_toolbar():
    import json
    prefs = json.loads((ROOT / "os/overlay/app-misc/posterchanos-shell/files/firefox-policies.json")
                       .read_text())["policies"]["Preferences"]
    assert prefs["browser.theme.toolbar-theme"] == {"Value": 0, "Status": "default"}
    assert prefs["browser.tabs.inTitlebar"]["Value"] == 0


# ------------------------------------------------------------------- the real thing, where possible

def _tools():
    need = ("wayfire", "foot", "grim", "g++", "wayland-scanner", "pkg-config")
    return all(shutil.which(t) for t in need)


@pytest.mark.skipif(not _tools(), reason="needs wayfire, foot, grim, g++, wayland-scanner and pkg-config")
def test_measured_in_a_headless_wayfire(tmp_path):
    """Builds the plugin exactly as the ebuild does, runs Wayfire headless with the shipped config and
    photographs four windows: server-decorated, client-decorated, a PosterChan surface, fullscreen."""
    PIL = pytest.importorskip("PIL.Image")
    protocols = subprocess.run(["pkg-config", "--variable=pkgdatadir", "wayland-protocols"],
                               capture_output=True, text=True, check=True).stdout.strip()
    for xml, header in (("unstable/pointer-constraints/pointer-constraints-unstable-v1.xml",
                         "pointer-constraints-unstable-v1-protocol.h"),
                        ("stable/xdg-shell/xdg-shell.xml", "xdg-shell-protocol.h")):
        subprocess.run(["wayland-scanner", "server-header", f"{protocols}/{xml}", str(tmp_path / header)], check=True)
    flags = subprocess.run(["pkg-config", "--cflags", "--libs", "wayfire"], capture_output=True, text=True).stdout.split()
    if not flags:
        pytest.skip("wayfire development headers are not installed")
    subprocess.run(["g++", "-std=c++17", "-fPIC", "-shared", f"-I{tmp_path}", *flags[:0],
                    *[f for f in flags if f.startswith(("-I", "-D"))],
                    str(PLUGIN_DIR / "files/posterchan-shell.cpp"), "-o", str(tmp_path / "libposterchan-shell.so"),
                    *[f for f in flags if not f.startswith(("-I", "-D"))]], check=True, capture_output=True, timeout=300)
    shutil.copy(PLUGIN_DIR / "files/posterchan-shell.xml", tmp_path)
    foot = f"env XDG_CONFIG_HOME={tmp_path}/nocfg /usr/bin/foot"
    shots = {}
    scenes = {
        "framed": ("ssd = " + foot + " --window-size-pixels=500x300 -T ssd-foot -e sleep 60\n"
                   "csd = sleep 2; " + foot + " --window-size-pixels=500x300 -o csd.preferred=client -T csd-foot -e sleep 60\n"),
        "excluded": ("pc = " + foot + " --app-id place.poster.desktop -T pcwin -e sleep 60\n"
                     "fs = sleep 2; " + foot + " --fullscreen -T fsfoot -e sleep 60\n"),
    }
    rules = ('rule_a = on created if title is "ssd-foot" then set geometry 40 60 500 300\n'
             'rule_b = on created if title is "csd-foot" then set geometry 640 60 500 300\n'
             'rule_c = on created if title is "pcwin" then set geometry 40 60 500 300\n')
    for name, autostart in scenes.items():
        config = re.sub(r"(?ms)^\[autostart\]\n.*?(?=^\[)", "[autostart]\nautostart_wf_shell = false\n" + autostart + "\n", INI)
        config = config.replace("[window-rules]\n", "[window-rules]\n" + rules, 1)
        (tmp_path / f"{name}.ini").write_text(config)
        runtime = tempfile.mkdtemp(prefix="pc-wf-")
        os.chmod(runtime, 0o700)
        env = {**os.environ, "XDG_RUNTIME_DIR": runtime, "WLR_BACKENDS": "headless", "WLR_RENDERER": "pixman",
               "WLR_HEADLESS_OUTPUTS": "1", "WLR_LIBINPUT_NO_DEVICES": "1",
               "WAYFIRE_PLUGIN_PATH": str(tmp_path), "WAYFIRE_PLUGIN_XML_PATH": str(tmp_path)}
        env.pop("WAYLAND_DISPLAY", None); env.pop("DISPLAY", None)
        proc = subprocess.Popen(["wayfire", "-c", str(tmp_path / f"{name}.ini")], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            deadline = time.time() + 30
            display = None
            while time.time() < deadline and not display:
                display = next((p for p in os.listdir(runtime) if re.fullmatch(r"wayland-\d+", p)), None)
                time.sleep(.2)
            assert display, "wayfire did not start headless"
            time.sleep(6)
            shot = tmp_path / f"{name}.png"
            subprocess.run(["grim", str(shot)], env={**env, "WAYLAND_DISPLAY": display}, check=True, timeout=20)
            shots[name] = PIL.open(shot).convert("RGB")
        finally:
            os.killpg(proc.pid, 15)
            proc.wait(timeout=10)
            shutil.rmtree(runtime, ignore_errors=True)
    active = _hex(_option("posterchan-shell", "window_border_active_color")[:7])
    inactive = _hex(_option("posterchan-shell", "window_border_inactive_color")[:7])
    im = shots["framed"]
    # The focused (client-decorated, mapped last) window: all four edges, INCLUDING its own title bar.
    csd = {"top": im.getpixel((890, 91)), "left": im.getpixel((641, 200)),
           "right": im.getpixel((1138, 200)), "bottom": im.getpixel((890, 355))}
    assert set(csd.values()) == {active}, csd
    ssd = {"top": im.getpixel((290, 61)), "left": im.getpixel((41, 200)),
           "right": im.getpixel((538, 200)), "bottom": im.getpixel((290, 356))}
    assert set(ssd.values()) == {inactive}, ssd
    assert im.getpixel((290, 78)) == _hex(_root_token("bg")), "the unfocused title bar is not the page colour"
    colours = set(shots["excluded"].getdata())
    assert active not in colours and inactive not in colours, "a PosterChan surface or a fullscreen window was framed"
