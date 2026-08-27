#!/usr/bin/env python3
"""Require one full-output PosterChan surface for every active installed Sway output."""
from __future__ import annotations

import json
import os
import subprocess
import sys


APP_IDS = {"place.poster.desktop", "posterchan-desktop"}


def walk(node):
    yield node
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        yield from walk(child)


def validate(outputs, tree):
    active = [o for o in outputs if o.get("active") and o.get("rect", {}).get("width", 0) > 0]
    surfaces = [n for n in walk(tree) if n.get("app_id") in APP_IDS]
    failures = []
    for output in active:
        rect = output["rect"]
        exact = [n for n in surfaces if n.get("rect") == rect and n.get("visible") is not False]
        if len(exact) != 1:
            failures.append(f"{output.get('name', '?')} has {len(exact)} full visible shell surfaces")
    covered = {tuple(n.get("rect", {}).get(k, 0) for k in ("x", "y", "width", "height"))
               for n in surfaces}
    expected = {tuple(o["rect"].get(k, 0) for k in ("x", "y", "width", "height"))
                for o in active}
    extras = covered - expected
    if extras:
        failures.append(f"{len(extras)} shell surface geometries do not belong to an active output")
    if failures:
        raise RuntimeError("; ".join(failures))
    return {"outputs": len(active), "surfaces": len(surfaces),
            "geometry": [f"{o['rect']['width']}x{o['rect']['height']}" for o in active]}


def session_env():
    env = os.environ.copy()
    got = subprocess.run(["systemctl", "--user", "show-environment"], text=True,
                         capture_output=True, check=True)
    for line in got.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            if key in {"SWAYSOCK", "I3SOCK", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "DISPLAY"}:
                env[key] = value
    return env


def sway(kind, env):
    got = subprocess.run(["swaymsg", "-t", kind, "-r"], env=env, text=True,
                         capture_output=True, check=True)
    return json.loads(got.stdout)


def main():
    owned = subprocess.run(["qfile", "-q", "/opt/posterchan/resources/app.asar"],
                           stdout=subprocess.DEVNULL)
    if owned.returncode:
        raise RuntimeError("installed app.asar is not package-owned")
    env = session_env()
    result = validate(sway("get_outputs", env), sway("get_tree", env))
    print("Installed shell output gate passed: " + json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Installed shell output gate failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
