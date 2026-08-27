#!/usr/bin/env python3
"""Installed PosterChanOS native-focus gate. Caller creates and later removes the disposable app."""

import json
import os
import re
import subprocess
import sys
import time


SAFE = re.compile(r"(?:probe|disposable|test)", re.I)


def run(*args, binary=False):
    p = subprocess.run(args, capture_output=True, timeout=20)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout).decode(errors="replace")[-1000:])
    return p.stdout if binary else p.stdout.decode()


def descendants(node, output=None):
    if node.get("type") == "output":
        output = node.get("name")
    yield node, output
    for key in ("nodes", "floating_nodes"):
        for child in node.get(key) or []:
            yield from descendants(child, output)


def resolve(tree, app_id, pid):
    if not app_id or not SAFE.search(app_id):
        raise ValueError("PC_NATIVE_APP_ID must explicitly identify a disposable probe/test app")
    hits = [(n, out) for n, out in descendants(tree)
            if str(n.get("app_id") or "") == app_id and int(n.get("pid") or -1) == pid]
    if len(hits) != 1:
        raise ValueError(f"refusing ambiguous target: {len(hits)} windows match app_id+pid")
    return hits[0]


def ppm_stats(data):
    try:
        pixels = data.split(b"\n", 3)[3]
    except IndexError as exc:
        raise ValueError("grim did not return PPM pixels") from exc
    if not pixels:
        raise ValueError("empty screenshot")
    mean = sum(pixels) / len(pixels)
    variance = sum(x * x for x in pixels) / len(pixels) - mean * mean
    return mean, variance, sum(x < 24 for x in pixels) / len(pixels)


def tree():
    return json.loads(run("swaymsg", "-t", "get_tree", "-r"))


def outputs():
    return [x["name"] for x in json.loads(run("swaymsg", "-t", "get_outputs", "-r"))
            if x.get("active")]


def geometry(rect, inset=True):
    x, y, w, h = (int(rect[k]) for k in ("x", "y", "width", "height"))
    if inset and w > 80 and h > 140:
        x += 16; y += 80; w -= 32; h -= 96
    return f"{x},{y} {w}x{h}"


def main():
    app_id = os.environ.get("PC_NATIVE_APP_ID", "")
    try:
        pid = int(os.environ.get("PC_NATIVE_PID", ""))
    except ValueError:
        print("FAIL PC_NATIVE_PID must be an explicit integer", file=sys.stderr); return 2
    cycles = max(2, min(20, int(os.environ.get("PC_NATIVE_CYCLES", "4"))))
    first_tree = tree()
    target, original_output = resolve(first_tree, app_id, pid)
    original = dict(target["rect"])
    outs = outputs()
    if len(outs) < 1:
        raise RuntimeError("no active outputs")
    tested = []
    try:
        for out in outs[:2]:
            run("swaymsg", "-q", f"[con_id={target['id']}] move container to output {out}")
            time.sleep(.25)
            for i in range(cycles):
                current_tree = tree()
                target, actual_out = resolve(current_tree, app_id, pid)
                shells = [(n, o) for n, o in descendants(current_tree)
                          if o == actual_out and re.search(r"^(?:place\.poster\.desktop|posterchan)",
                                                           str(n.get("app_id") or ""), re.I)]
                if len(shells) != 1:
                    raise RuntimeError(f"need one managed shell on {actual_out}, found {len(shells)}")
                before = dict(target["rect"])
                run("swaymsg", "-q", f"[con_id={shells[0][0]['id']}] focus")
                time.sleep(.12)
                parked, _ = resolve(tree(), app_id, pid)
                pixels = run("grim", "-g", geometry(parked["rect"]), "-t", "ppm", "-", binary=True)
                mean, variance, nearblack = ppm_stats(pixels)
                if mean < 45 or variance < 250 or nearblack > .92:
                    raise AssertionError(f"black/flat parked frame on {actual_out}: mean={mean:.2f} "
                                         f"variance={variance:.2f} nearblack={nearblack:.3f}")
                run("swaymsg", "-q", f"[con_id={target['id']}] focus")
                time.sleep(.15)
                restored, _ = resolve(tree(), app_id, pid)
                if not restored.get("visible") or restored.get("scratchpad_state") not in (None, "none"):
                    raise AssertionError(f"surface did not restore: visible={restored.get('visible')} "
                                         f"scratchpad={restored.get('scratchpad_state')}")
                if restored["rect"] != before:
                    raise AssertionError(f"focus changed geometry: {before} -> {restored['rect']}")
                tested.append({"output": actual_out, "cycle": i + 1, "mean": round(mean, 2),
                               "variance": round(variance, 2), "nearblack": round(nearblack, 4)})
    finally:
        if original_output:
            run("swaymsg", "-q", f"[con_id={target['id']}] move container to output {original_output}")
            run("swaymsg", "-q", f"[con_id={target['id']}] move position {original['x']} {original['y']}")
            run("swaymsg", "-q", f"[con_id={target['id']}] resize set {original['width']} {original['height']}")
    resolve(tree(), app_id, pid)  # cleanup remains caller-owned; prove the same probe still exists
    print(json.dumps({"ok": True, "app_id": app_id, "pid": pid, "cycles": tested}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("FAIL", str(exc), file=sys.stderr)
        raise SystemExit(1)
