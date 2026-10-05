"""Super+G tiles and Super alone opens Start -- in a real Wayfire, with the SHIPPED Start binding.

Reported as "super+g is loading the start menu instead of tiling". Two causes, both measured in a
headless Wayfire 0.10.1 driven by tests/fixtures/wayfire_twokb.c (each zwp_virtual_keyboard_v1 is its
own input device, which is exactly the shape of the desk's keyboard):

  1. `release_binding_start = KEY_LEFTMETA` arms when Super goes DOWN, and the command plugin refuses
     every other binding while it is armed -- so no Super shortcut ever ran, on any keyboard.
  2. An ASUS ROG Strix Scope II 96 in N-key rollover reports Super on one interface and G on another;
     wlroots keeps modifiers per device, so Super+G arrived as a bare G
     (os/overlay/gui-wm/wayfire/files/wayfire-0.10.1-seat-wide-modifiers.patch).

Needs a Wayfire build with the overlay's patches: PC_WAYFIRE_BUILD=<source tree with build/>.
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
BUILD = os.environ.get("PC_WAYFIRE_BUILD", "")
pytestmark = pytest.mark.skipif(not (BUILD and Path(BUILD, "build/src/wayfire").exists()),
                                reason="PC_WAYFIRE_BUILD must name a built, patched Wayfire 0.10.1 tree")


def _shipped_start_binding():
    ini = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/wayfire.ini").read_text()
    section = ini.split("\n[command]\n", 1)[1].split("\n[", 1)[0]
    found = [l.split("=", 1) for l in section.splitlines() if re.match(r"^\w*binding_start\s*=", l)]
    assert len(found) == 1, found
    return found[0][0].strip(), found[0][1].strip()


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    out = tmp_path_factory.mktemp("twokb")
    xml = Path(BUILD, "subprojects/wlroots/protocol/virtual-keyboard-unstable-v1.xml")
    subprocess.run(["wayland-scanner", "client-header", str(xml), str(out / "vk.h")], check=True)
    subprocess.run(["wayland-scanner", "private-code", str(xml), str(out / "vk.c")], check=True)
    shutil.copy(ROOT / "tests/fixtures/wayfire_twokb.c", out / "twokb.c")
    flags = subprocess.run(["pkg-config", "--cflags", "--libs", "wayland-client", "xkbcommon"],
                           capture_output=True, text=True, check=True).stdout.split()
    subprocess.run(["gcc", "-O1", "-o", str(out / "twokb"), str(out / "twokb.c"), str(out / "vk.c"), *flags],
                   check=True, cwd=out)
    return out / "twokb"


def _press(client, mode):
    """Run one gesture against a fresh headless Wayfire; which commands ran?"""
    key, chord = _shipped_start_binding()
    with tempfile.TemporaryDirectory() as d:
        Path(d, "wf.ini").write_text(
            "[core]\nplugins = command\n[command]\n"
            f"binding_combo = <super> KEY_G\ncommand_combo = touch {d}/combo\n"
            f"{key} = {chord}\ncommand_start = touch {d}/start\n")
        env = dict(os.environ, XDG_RUNTIME_DIR=d, WLR_BACKENDS="headless", WLR_HEADLESS_OUTPUTS="1",
                   WLR_RENDERER="pixman", WLR_LIBINPUT_NO_DEVICES="1",
                   WAYFIRE_PLUGIN_PATH=f"{BUILD}/build/plugins/single_plugins",
                   WAYFIRE_PLUGIN_XML_PATH=f"{BUILD}/metadata")
        wf = subprocess.Popen([f"{BUILD}/build/src/wayfire", "-B", f"{BUILD}/build/src/libdefault-config-backend.so",
                               "-c", f"{d}/wf.ini"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                sockets = [p.name for p in Path(d).iterdir() if re.fullmatch(r"wayland-\d+", p.name)]
                if sockets: break
                time.sleep(0.05)
            assert sockets, "Wayfire did not start"
            subprocess.run([str(client), mode], env=dict(env, WAYLAND_DISPLAY=sockets[0]), check=True, timeout=20)
            time.sleep(0.5)
            return {"combo": Path(d, "combo").exists(), "start": Path(d, "start").exists()}
        finally:
            wf.terminate(); wf.wait(timeout=10)


@pytest.mark.parametrize("mode", ["same", "split"])
def test_super_g_tiles_and_does_not_open_start(client, mode):
    assert _press(client, mode) == {"combo": True, "start": False}, mode


def test_super_alone_opens_start(client):
    assert _press(client, "tap") == {"combo": False, "start": True}
