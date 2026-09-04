#!/usr/bin/env python3
"""Exercise the installed PosterChan VM backend against an explicit disposable domain.

The gate intentionally does not create or delete a VM.  Pass the name of a disposable domain that
already contains an installed system and still has its installer ISO attached.  It proves both
boots through the packaged app.asar backend and requires a real virt-viewer surface in Sway.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wayfire_ipc as wf  # noqa: E402

try:  # package import under pytest / repo tooling
    from scripts.check_livecd_vm import frame_is_graphical
except ModuleNotFoundError:  # direct ``python scripts/check_installed_vm.py`` release invocation
    from check_livecd_vm import frame_is_graphical


def command(args, env=None, timeout=40):
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, env=env)


def session_environment():
    env = os.environ.copy()
    got = command(["systemctl", "--user", "show-environment"])
    for line in got.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            if key in {"DISPLAY", "WAYLAND_DISPLAY", "WAYFIRE_SOCKET", "XDG_RUNTIME_DIR",
                       "DBUS_SESSION_BUS_ADDRESS"}:
                env[key] = value
    return env


def backend(binary: Path, asar: Path, expression: str, env):
    source = (
        f"const v=require({json.dumps(str(asar) + '/vm.js')});"
        f"(async()=>console.log(JSON.stringify(await ({expression}))))()"
        ".catch(e=>{console.error(e);process.exit(1)})"
    )
    child_env = dict(env, ELECTRON_RUN_AS_NODE="1")
    result = command([str(binary), "-e", source], child_env, 120)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "installed VM backend failed")
    return json.loads(result.stdout.strip().splitlines()[-1])


def wait_state(binary, asar, name, wanted, env, seconds=120):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        detail = backend(binary, asar, f"v.details({json.dumps(name)})", env)
        if detail.get("ok") and wanted in detail.get("state", ""):
            return detail
        time.sleep(1)
    raise RuntimeError(f"VM did not become {wanted}")


def is_viewer_surface(node, name):
    """Match virt-viewer on both native Wayland and its supported XWayland fallback.

    Reads BOTH shapes on purpose: Wayfire answers `app-id`/`title`/`geometry`, Sway answered
    `app_id`/`name`/`rect` inside `window_properties`. The gate's own fakes are written in the Sway
    shape and the tests that drive them are the reason this stayed readable.
    """
    props = node.get("window_properties") or {}
    identity = " ".join(str(value or "") for value in (
        node.get("app-id"), node.get("app_id"), props.get("class"), props.get("instance")
    )).casefold()
    title = str(node.get("title") or node.get("name") or props.get("title") or "").casefold()
    rect = node.get("geometry") or node.get("rect") or {}
    return ("virt-viewer" in identity and name.casefold() in title
            and rect.get("width", 0) >= 640 and rect.get("height", 0) >= 400)


def viewer_frame_is_graphical(node, env, runner=command):
    """Prove the mapped viewer contains guest pixels, not merely a black client window.

    Crop the compositor decoration and virt-viewer's chrome before applying the same framebuffer
    classifier as the ISO gate.  A mapped virt-viewer surface appears several seconds before the
    firmware/guest draws; treating mapping as a successful boot made both halves of this gate pass
    against a permanently black guest.
    """
    rect = node.get("geometry") or node.get("rect") or {}
    x, y = int(rect.get("x", 0)) + 8, int(rect.get("y", 0)) + 40
    width, height = int(rect.get("width", 0)) - 16, int(rect.get("height", 0)) - 48
    if width < 600 or height < 340:
        return False
    with tempfile.TemporaryDirectory(prefix="pc-vm-viewer-") as raw:
        shot = Path(raw) / "guest.ppm"
        captured = runner(["grim", "-t", "ppm", "-g", f"{x},{y} {width}x{height}", str(shot)],
                          env=env, timeout=10)
        if captured.returncode or not shot.is_file():
            return False
        try:
            return frame_is_graphical(shot)
        except (OSError, ValueError):
            return False


def view_listing(args=None, env=None, timeout=10):
    """The compositor's windows, in the CompletedProcess shape `tree_runner` is injected as.

    This was `swaymsg -t get_tree -r`, which on the Wayfire session exits "Unable to retrieve socket
    path" -- so the gate could see no virt-viewer surface, ever, and reported a VM that never became
    visible on a desktop showing it perfectly.
    """
    try:
        payload = json.dumps(wf.views(wf.socket_path(env)))
    except Exception as exc:                     # no session, or the socket went away mid-run
        return subprocess.CompletedProcess(args or [], 1, "", str(exc))
    return subprocess.CompletedProcess(args or [], 0, payload, "")


def visible_viewer(name, env, seconds=60, stable_samples=6, interval=1,
                   tree_runner=view_listing, frame_probe=viewer_frame_is_graphical):
    """Require a sustained graphical guest surface, not one lucky rendered frame."""
    if stable_samples < 2:
        raise ValueError("stable_samples must be at least 2")
    deadline = time.monotonic() + seconds
    graphical_seen = False
    stable = 0
    while time.monotonic() < deadline:
        # WAYFIRE ANSWERS A FLAT LIST. Sway answered a tree and this walked it; the walk is kept
        # because `tree_runner` is what the unit tests inject, and a Sway-shaped fake is still a
        # valid description of "these windows exist".
        listing = tree_runner([], env=env)
        viewer = None
        if listing.returncode == 0:
            payload = json.loads(listing.stdout)
            nodes = payload if isinstance(payload, list) else [payload]
            while nodes:
                node = nodes.pop()
                if isinstance(node, dict):
                    nodes.extend(node.get("nodes", []) + node.get("floating_nodes", []))
                    if is_viewer_surface(node, name):
                        viewer = node
                        break
        if viewer is not None:
            graphical = frame_probe(viewer, env)
            if graphical_seen and not graphical:
                raise RuntimeError("virt-viewer guest frame became black after graphics appeared")
            if graphical:
                graphical_seen = True
                stable += 1
                if stable >= stable_samples:
                    return viewer.get("geometry") or viewer.get("rect") or {}
            else:
                stable = 0
        elif graphical_seen and listing.returncode == 0:
            raise RuntimeError("virt-viewer disappeared after graphics appeared")
        time.sleep(interval)
    raise RuntimeError("virt-viewer never sustained a usable graphical compositor surface")


def start_and_view(binary, asar, name, env):
    started = backend(binary, asar, f"v.action({json.dumps(name)},'start')", env)
    if not started.get("ok"):
        raise RuntimeError(started.get("error") or "VM did not start")
    wait_state(binary, asar, name, "running", env)
    viewed = backend(binary, asar, f"v.view({json.dumps(name)})", env)
    if not viewed.get("ok"):
        raise RuntimeError(viewed.get("error") or "viewer did not start")
    return visible_viewer(name, env)


def shutdown(binary, asar, name, env, force_after_timeout=False):
    result = backend(binary, asar, f"v.action({json.dumps(name)},'shutdown')", env)
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "VM did not accept shutdown")
    try:
        return wait_state(binary, asar, name, "shut off", env)
    except RuntimeError:
        if not force_after_timeout:
            raise
        # Live installers do not all handle the virtual ACPI button.  This gate is restricted to an
        # explicitly acknowledged disposable domain, so exercise the UI's named Force Off backend
        # after the same bounded wait.  The product's ordinary bootDisk path remains fail-safe.
        stopped = backend(binary, asar, f"v.action({json.dumps(name)},'stop')", env)
        if not stopped.get("ok"):
            raise RuntimeError(stopped.get("error") or "disposable VM could not be forced off")
        return wait_state(binary, asar, name, "shut off", env, 30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="explicit disposable libvirt domain")
    parser.add_argument("--expected-iso", type=Path, required=True)
    parser.add_argument("--binary", type=Path, default=Path("/opt/posterchan/posterchan-desktop"))
    parser.add_argument("--asar", type=Path, default=Path("/opt/posterchan/resources/app.asar"))
    parser.add_argument("--eject-and-boot", action="store_true", required=True,
                        help="acknowledge that the domain's persistent CD source will be ejected")
    args = parser.parse_args()
    if not args.binary.is_file() or not args.asar.is_file() or not args.expected_iso.is_file():
        parser.error("installed binary, app.asar, and expected ISO must exist")

    env = session_environment()
    initial = backend(args.binary, args.asar, f"v.details({json.dumps(args.name)})", env)
    if not initial.get("ok") or "shut off" not in initial.get("state", ""):
        raise RuntimeError("the disposable VM must begin shut off")
    cd = next((x for x in initial.get("disks", []) if x.get("device") == "cdrom"), None)
    if not cd or Path(cd.get("source", "")).resolve() != args.expected_iso.resolve():
        raise RuntimeError("the expected installer ISO is not attached")
    live_rect = start_and_view(args.binary, args.asar, args.name, env)
    shutdown(args.binary, args.asar, args.name, env, force_after_timeout=True)

    selected = backend(args.binary, args.asar, f"v.bootDisk({json.dumps(args.name)})", env)
    if not selected.get("ok"):
        raise RuntimeError(selected.get("error") or "installed disk was not selected")
    after = backend(args.binary, args.asar, f"v.details({json.dumps(args.name)})", env)
    cd = next((x for x in after.get("disks", []) if x.get("device") == "cdrom"), None)
    if after.get("bootOrder") != "disk" or not cd or cd.get("source") != "-":
        raise RuntimeError("installer media remained attached or disk-first did not persist")
    disk_rect = start_and_view(args.binary, args.asar, args.name, env)
    print(json.dumps({"ok": True, "liveViewer": live_rect, "diskViewer": disk_rect,
                      "media": cd.get("source"), "bootOrder": after.get("bootOrder")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
