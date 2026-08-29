#!/usr/bin/env python3
"""Stress a real installed Foot surface while Sway reconfigures it.

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


def skip(why: str) -> int:
    print("SKIP installed Foot flicker gate: " + why)
    return 2


def sway(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["swaymsg", *args], text=True, capture_output=True, timeout=15,
                          check=check)


def tree() -> dict:
    return json.loads(sway("-t", "get_tree", "-r").stdout)


def leaves(node: dict):
    if node.get("pid") or node.get("app_id") or (node.get("window_properties") or {}).get("class"):
        yield node
    for child in (node.get("nodes") or []) + (node.get("floating_nodes") or []):
        yield from leaves(child)


def foot_node(marker: str):
    for node in leaves(tree()):
        props = node.get("window_properties") or {}
        if marker in str(node.get("name") or "") or marker in str(props.get("title") or ""):
            return node
    return None


def output_for(rect: dict, outputs: list[dict]):
    cx = float(rect.get("x", 0)) + float(rect.get("width", 0)) / 2
    cy = float(rect.get("y", 0)) + float(rect.get("height", 0)) / 2
    for output in outputs:
        r = output.get("rect") or {}
        if r.get("x", 0) <= cx < r.get("x", 0) + r.get("width", 0) \
                and r.get("y", 0) <= cy < r.get("y", 0) + r.get("height", 0):
            return output
    return None


def capture(node: dict, target: Path, label: str) -> tuple[float, int]:
    from PIL import Image, ImageStat

    r = node.get("rect") or {}
    w, h = int(r.get("width", 0)), int(r.get("height", 0))
    if w < 100 or h < 80:
        raise AssertionError(f"{label}: Foot collapsed to {w}x{h}")
    geometry = f"{int(r.get('x', 0))},{int(r.get('y', 0))} {w}x{h}"
    # PPM is part of grim's baseline build. PNG depends on optional pixman/libpng support and the
    # lean PosterChanOS image deliberately may not provide it; Pillow reads PPM directly.
    proc = subprocess.Popen(["grim", "-t", "ppm", "-g", geometry, str(target)], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(0.08)
    # grim 1.5 waits for a damaged frame; zero-distance motion wakes the cursor plane without
    # changing pointer position or focus, matching the installed screenshot helper.
    sway("-q", "seat seat0 cursor move 0 0", check=False)
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
    if not os.environ.get("SWAYSOCK"):
        return skip("SWAYSOCK is not available")
    for binary in ("swaymsg", "foot", "grim"):
        if not shutil.which(binary):
            return skip(binary + " is not installed")
    try:
        import PIL  # noqa: F401
    except ImportError:
        return skip("Pillow is required to inspect compositor captures")

    outputs = json.loads(sway("-t", "get_outputs", "-r").stdout)
    outputs = [o for o in outputs if o.get("active") and (o.get("rect") or {}).get("width")]
    if not outputs:
        return skip("Sway reports no active output")

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
    previous_focus = next((n.get("id") for n in leaves(tree()) if n.get("focused")), None)
    try:
        node = settle(marker)
        if not node:
            raise AssertionError("Foot never mapped a window")
        cid = int(node["id"])
        original = dict(node.get("rect") or {})
        original_output = output_for(original, outputs)
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

            other = next((n for n in leaves(tree()) if n.get("id") != cid and n.get("type") == "con"), None)
            if other:
                sway(f"[con_id={int(other['id'])}] focus")
                time.sleep(0.12); shot("focus-away")
            sway(f"[con_id={cid}] focus")
            time.sleep(0.12); shot("focus-return")

            ow, oh = int(original["width"]), int(original["height"])
            for i, (w, h) in enumerate(((max(420, ow - 97), max(260, oh - 61)),
                                         (max(430, ow + 53), max(270, oh + 37)), (ow, oh))):
                # One Sway IPC transaction: no intermediate floating/default geometry is exposed.
                sway(f"[con_id={cid}] floating enable, resize set {w} {h}, move absolute position "
                     f"{int(original['x'])} {int(original['y'])}")
                time.sleep(0.14); shot("resize-" + str(i))

            destination = next((o for o in outputs if o.get("name") != original_output.get("name")), None)
            if destination:
                sway(f"[con_id={cid}] move container to output {destination['name']}")
                time.sleep(0.25); shot("other-output")
                sway(f"[con_id={cid}] move container to output {original_output['name']}, floating enable, "
                     f"resize set {ow} {oh}, move absolute position {int(original['x'])} {int(original['y'])}")
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
            sway(f"[con_id={int(previous_focus)}] focus", check=False)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print("FAIL installed Foot flicker gate: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
