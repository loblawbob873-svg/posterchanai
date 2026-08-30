#!/usr/bin/env python3
"""A REAL compositor for tests, with as many outputs as the test needs.

    from swayheadless import headless_sway
    with headless_sway(outputs=[(1920, 1080), (1600, 900)]) as sway:
        print(sway.outputs())          # two real sway outputs
        sway.msg("exec", "firefox")

WHY THIS EXISTS. Every window-manager bug reported against PosterChanOS so far has been invisible
here: windows resetting when resized, moved between monitors, activated or maximised; a native app
drawn separately from its PosterChan frame; a terminal that glitches crossing a seam. The checks
covering that area all pass, because they drive a STUB compositor with ONE screen, and the failures
live in the half that talks to a real one — with two outputs, different sizes, and real focus.

wlroots can do this with no hardware and no VM: WLR_BACKENDS=headless gives a compositor with
virtual outputs, and sway's ordinary IPC works against it. Measured on this machine: two outputs up
in about six seconds, no GPU, no display, ~40 MB.

It is deliberately NOT a fixture that assumes a desktop is present. `available()` says whether this
machine can run one at all, so a check can SKIP with a reason instead of failing for the wrong one.
"""
from contextlib import contextmanager
from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile
import time


def available():
    """(ok, why-not). sway and a writable XDG_RUNTIME_DIR are the whole requirement."""
    if not shutil.which("sway"):
        return False, "sway is not installed on this machine"
    if not shutil.which("swaymsg"):
        return False, "swaymsg is not installed on this machine"
    run = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
    if not os.path.isdir(run) or not os.access(run, os.W_OK):
        return False, "no writable XDG_RUNTIME_DIR (%s)" % run
    return True, ""


class Sway:
    def __init__(self, sock, proc, log):
        self.sock, self.proc, self.log = sock, proc, log

    def msg(self, *args):
        """swaymsg against THIS instance. Returns parsed JSON when the reply is JSON."""
        r = subprocess.run(["swaymsg", "-s", self.sock, *args],
                           capture_output=True, text=True, timeout=30)
        out = r.stdout.strip()
        try:
            return json.loads(out)
        except Exception:
            return out

    def outputs(self):
        return self.msg("-t", "get_outputs")

    def tree(self):
        return self.msg("-t", "get_tree")

    def windows(self):
        """Every view sway knows about, flattened — app id, geometry and which output it is on."""
        found = []

        def walk(node, output):
            name = node.get("name") if node.get("type") == "output" else output
            if node.get("app_id") or node.get("window_properties"):
                r = node.get("rect", {})
                found.append({
                    "id": node.get("id"),
                    "app": node.get("app_id")
                           or (node.get("window_properties") or {}).get("class"),
                    "title": node.get("name") or "",
                    "output": name,
                    "x": r.get("x"), "y": r.get("y"),
                    "w": r.get("width"), "h": r.get("height"),
                    "floating": node.get("type") == "floating_con",
                })
            for key in ("nodes", "floating_nodes"):
                for child in node.get(key, []) or []:
                    walk(child, name)

        walk(self.tree(), None)
        return found


@contextmanager
def headless_sway(outputs=((1920, 1080),), extra_config=""):
    """Run a headless sway with one virtual output per (width, height), and clean it up."""
    ok, why = available()
    if not ok:
        raise RuntimeError(why)
    run = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
    lines = ["default_border none", "default_floating_border none"]
    at = 0
    for i, (w, h) in enumerate(outputs, start=1):
        lines.append("output HEADLESS-%d mode %dx%d position %d,0" % (i, w, h, at))
        at += w
    lines.append(extra_config)
    tmp = tempfile.mkdtemp(prefix="swayheadless-")
    conf = Path(tmp, "config")
    conf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log = open(Path(tmp, "sway.log"), "w+")
    env = dict(os.environ,
               WLR_BACKENDS="headless",
               WLR_LIBINPUT_NO_DEVICES="1",
               WLR_HEADLESS_OUTPUTS=str(len(outputs)),
               XDG_RUNTIME_DIR=run)
    # No WAYLAND_DISPLAY inherited: a sway started inside another compositor nests instead of
    # creating its own outputs, and then this returns one screen of the wrong size.
    env.pop("WAYLAND_DISPLAY", None)
    env.pop("DISPLAY", None)
    before = set(Path(run).glob("sway-ipc.*.sock"))
    proc = subprocess.Popen(["sway", "-c", str(conf)], env=env, stdout=log, stderr=log)
    sock = None
    try:
        # The socket is named with sway's pid, but the child may re-exec; take whichever is new.
        for _ in range(120):
            time.sleep(0.25)
            fresh = [p for p in Path(run).glob("sway-ipc.*.sock") if p not in before]
            if fresh:
                sock = str(sorted(fresh, key=lambda p: p.stat().st_mtime)[-1])
                break
            if proc.poll() is not None:
                log.seek(0)
                raise RuntimeError("sway exited: " + log.read()[-500:])
        if not sock:
            raise RuntimeError("sway never opened an IPC socket")
        sway = Sway(sock, proc, log)
        for _ in range(40):                      # outputs appear a moment after the socket
            if len(sway.outputs() or []) >= len(outputs):
                break
            time.sleep(0.25)
        yield sway
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    ok, why = available()
    if not ok:
        print("SKIP", why)
        raise SystemExit(2)
    with headless_sway(outputs=[(1920, 1080), (1600, 900)]) as s:
        for o in s.outputs():
            r = o["rect"]
            print("%-12s %dx%d at %d,%d scale=%s"
                  % (o["name"], r["width"], r["height"], r["x"], r["y"], o.get("scale")))
        print("windows:", len(s.windows()))
