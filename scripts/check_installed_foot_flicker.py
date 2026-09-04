#!/usr/bin/env python3
"""Stress a real installed Foot surface while the compositor reconfigures it.

PORTED FROM SWAY. This gate demanded `SWAYSOCK` and drove `swaymsg`, so on the Wayfire session it
could only ever answer `SKIP SWAYSOCK is not available` -- a release gate that had quietly stopped
being able to pass or fail. The compositor calls go through scripts/wayfire_ipc.py now; everything
it measures (real pixels through grim, across focus, resize and a second output) is unchanged.

This is intentionally an installed compositor check, not a DOM simulation.  It creates one
disposable uniquely titled Foot, streams ANSI output continuously, captures the compositor's real
pixels through grim, and exercises focus, resize and (when present) another output.  It reads no
terminal content or user windows; screenshots live in a temporary directory and are removed.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wayfire_ipc as wf  # noqa: E402


def skip(why: str) -> int:
    print("SKIP installed Foot flicker gate: " + why)
    return 2


def leaves():
    """Wayfire answers a FLAT list of views. Sway answered a tree and this walked it."""
    return [v for v in wf.views() if v.get("mapped", True)]


def foot_node(marker: str):
    return next((v for v in leaves() if marker in wf.title_of(v)), None)


def focus(view_id: int):
    wf.call("window-rules/focus-view", data={"id": int(view_id)})


def place(view_id: int, rect: dict, outputs: list, output_id=None):
    """Move/resize in ONE IPC transaction, so no intermediate geometry is ever on screen.

    configure-view geometry is OUTPUT-LOCAL. Passing global coordinates works on the left monitor
    and displaces every other one by its own offset -- the bug the shell already paid for once
    (`assignShell` in desktop/wm-wayfire.js). So the destination output is chosen from the global
    rectangle first, then the rectangle is translated into that output's space.
    """
    data = {"id": int(view_id)}
    target = None
    if output_id is not None:
        target = next((o for o in outputs if o.get("id") == output_id), None)
    if target is None:
        target = wf.output_for(rect, outputs)
    if target is not None:
        data["output_id"] = target.get("id")
        base = wf.rect_of(target)
    else:
        base = {"x": 0, "y": 0}
    data["geometry"] = {"x": int(rect["x"]) - base["x"], "y": int(rect["y"]) - base["y"],
                        "width": int(rect["width"]), "height": int(rect["height"])}
    wf.call("window-rules/configure-view", data=data)


def capture(node: dict, target: Path, label: str) -> tuple[float, int]:
    from PIL import Image, ImageStat

    r = wf.rect_of(node)
    w, h = int(r.get("width", 0)), int(r.get("height", 0))
    if w < 100 or h < 80:
        raise AssertionError(f"{label}: Foot collapsed to {w}x{h}")
    geometry = f"{int(r.get('x', 0))},{int(r.get('y', 0))} {w}x{h}"
    # PPM is part of grim's baseline build. PNG depends on optional pixman/libpng support and the
    # lean PosterChanOS image deliberately may not provide it; Pillow reads PPM directly.
    proc = subprocess.Popen(["grim", "-t", "ppm", "-g", geometry, str(target)], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(0.08)
    # grim waits for a DAMAGED frame. Sway was nudged with `seat0 cursor move 0 0` to wake the
    # cursor plane; Wayfire's IPC has no cursor command and needs none here -- the workload below
    # prints continuously, so this surface damages itself several times a second. A still surface
    # would need the nudge back, in some other form.
    try:
        stdout, stderr = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        raise AssertionError(f"{label}: grim timed out: {stderr.strip()}")
    got = subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
    if got.returncode:
        raise AssertionError(f"{label}: grim failed: {got.stderr.strip()}")
    with Image.open(target) as image:
        grey = image.convert("L")
        stat = ImageStat.Stat(grey)
        deviation = float(stat.stddev[0])
        lo, hi = grey.getextrema()
    if not math.isfinite(deviation) or deviation < 2.0 or hi - lo < 18:
        raise AssertionError(
            f"{label}: Foot surface is blank/flat (stddev={deviation:.2f}, range={lo}..{hi})")
    return deviation, hi - lo


def settle(marker: str, timeout: float = 12.0):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        node = foot_node(marker)
        if node and (node.get("rect") or {}).get("width", 0) > 100:
            return node
        time.sleep(0.15)
    return None


def main() -> int:
    try:
        wf.socket_path()
    except wf.PrerequisiteMissing as exc:
        return skip(str(exc))
    for binary in ("foot", "grim"):
        if not shutil.which(binary):
            return skip(binary + " is not installed")
    try:
        import PIL  # noqa: F401
    except ImportError:
        return skip("Pillow is required to inspect compositor captures")

    outputs = [o for o in wf.outputs() if wf.rect_of(o)["width"] > 0]
    if not outputs:
        return skip("Wayfire reports no active output")

    marker = "pc-foot-flicker-" + str(os.getpid())
    # Colour, cursor movement, line erasure and full scrolling exercise Foot's damage path while
    # avoiding private shell data. The process is killed in finally and never writes a file.
    workload = (
        "i=0; while :; do i=$((i+1)); "
        "printf '\\033[38;5;%sm%06d sustained Codex Claude output \\033[0m %080d\\r\\n' "
        '"$((i%255+1))" "$i" "$i"; '
        "[ $((i%41)) -eq 0 ] && printf '\\033[2K\\r repaint-%06d' \"$i\"; done"
    )
    proc = subprocess.Popen(["foot", "-T", marker, "sh", "-c", workload],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    previous_focus = next((v.get("id") for v in leaves() if v.get("activated")), None)
    try:
        node = settle(marker)
        if not node:
            raise AssertionError("Foot never mapped a window")
        cid = int(node["id"])
        original = wf.rect_of(node)
        original_output = wf.output_for(original, outputs)
        if not original_output:
            raise AssertionError("could not identify Foot's starting output")

        samples = []
        with tempfile.TemporaryDirectory(prefix="pc-foot-flicker-") as td:
            folder = Path(td)
            def shot(label: str):
                live = foot_node(marker)
                if not live:
                    raise AssertionError(label + ": Foot disappeared")
                samples.append((label, *capture(live, folder / f"{len(samples):02d}.ppm", label)))

            for i in range(3):
                shot("stream-baseline-" + str(i)); time.sleep(0.12)

            other = next((v for v in leaves() if v.get("id") != cid
                          and v.get("role") != "desktop-environment"), None)
            if other:
                focus(int(other["id"]))
                time.sleep(0.12); shot("focus-away")
            focus(cid)
            time.sleep(0.12); shot("focus-return")

            ow, oh = int(original["width"]), int(original["height"])
            for i, (w, h) in enumerate(((max(420, ow - 97), max(260, oh - 61)),
                                         (max(430, ow + 53), max(270, oh + 37)), (ow, oh))):
                place(cid, {"x": original["x"], "y": original["y"], "width": w, "height": h},
                      outputs)
                time.sleep(0.14); shot("resize-" + str(i))

            destination = next((o for o in outputs if o.get("name") != original_output.get("name")), None)
            if destination:
                dest = wf.rect_of(destination)
                place(cid, {"x": dest["x"], "y": dest["y"], "width": ow, "height": oh}, outputs,
                      output_id=destination.get("id"))
                time.sleep(0.25); shot("other-output")
                place(cid, {"x": original["x"], "y": original["y"], "width": ow, "height": oh},
                      outputs, output_id=original_output.get("id"))
                time.sleep(0.25); shot("output-return")
            else:
                print("INFO Foot flicker gate: one output; cross-output sample skipped")

        drivers = []
        for device in Path("/sys/class/drm").glob("card*/device/driver"):
            try: drivers.append(device.resolve().name)
            except OSError: pass
        print("OK installed Foot stayed nonblank across sustained output/focus/resize"
              + ("/multi-output" if len(outputs) > 1 else "")
              + f"; {len(samples)} captures; DRM={','.join(sorted(set(drivers))) or 'unknown'}")
        return 0
    finally:
        proc.terminate()
        try: proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait(timeout=3)
        if previous_focus:
            try:
                focus(int(previous_focus))
            except Exception:
                pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print("FAIL installed Foot flicker gate: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
