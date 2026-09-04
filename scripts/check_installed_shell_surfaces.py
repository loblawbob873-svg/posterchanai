#!/usr/bin/env python3
"""Require one full-output PosterChan surface for every active installed Wayfire output.

PORTED FROM SWAY, and the port is the point: this gate asked `systemctl --user show-environment` for
`SWAYSOCK` and ran `swaymsg`, so on the Wayfire session it could only ever answer

    SKIP no installed Sway IPC session; run this gate on the active PosterChanOS desktop

-- which is indistinguishable, in a report, from a machine nobody happened to run it on. It is one
of the release gates for the installed desktop and it had stopped being able to pass or fail.

Wayfire's IPC is a uint32 little-endian length followed by JSON: no i3 magic header, no message
type. `window-rules/list-outputs` and `window-rules/list-views` answer the two questions this needs.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys


APP_IDS = {"place.poster.desktop", "posterchan-desktop", "PosterChan"}


class PrerequisiteMissing(RuntimeError):
    """The installed package may exist, but there is no live Wayfire session to inspect."""


def rect_of(node):
    """Wayfire calls it `geometry`; keep Sway's key names so `validate` reads the same."""
    g = node.get("geometry") or node.get("rect") or {}
    return {k: int(g.get(k, 0) or 0) for k in ("x", "y", "width", "height")}


def app_of(node):
    return str(node.get("app-id") or node.get("app_id") or "")


def validate(outputs, views):
    active = [o for o in outputs if rect_of(o)["width"] > 0]
    # App windows deliberately share the desktop's app_id; they are told apart by the stable
    # ``PosterChan Window`` title prefix (the same contract wayfire.ini's `ignore_views` and
    # pc-window-snap use). Counting those as monitor shells makes this gate fail whenever any real
    # app is open. `role: desktop-environment` is Wayfire's own layer-shell marker -- a notification
    # popup is not a shell surface.
    surfaces = [v for v in views
                if app_of(v) in APP_IDS
                and v.get("mapped", True)
                and v.get("role") != "desktop-environment"
                and not str(v.get("title") or "").startswith("PosterChan Window")]
    failures = []
    for output in active:
        rect = rect_of(output)
        exact = [v for v in surfaces if rect_of(v) == rect]
        if len(exact) != 1:
            failures.append(f"{output.get('name', '?')} has {len(exact)} full visible shell surfaces")
    covered = {tuple(rect_of(v)[k] for k in ("x", "y", "width", "height")) for v in surfaces}
    expected = {tuple(rect_of(o)[k] for k in ("x", "y", "width", "height")) for o in active}
    extras = covered - expected
    if extras:
        failures.append(f"{len(extras)} shell surface geometries do not belong to an active output")
    if failures:
        raise RuntimeError("; ".join(failures))
    return {"outputs": len(active), "surfaces": len(surfaces),
            "geometry": [f"{rect_of(o)['width']}x{rect_of(o)['height']}" for o in active]}


def socket_path():
    """The session's own IPC socket, from this environment or from the session manager's."""
    path = os.environ.get("WAYFIRE_SOCKET") or ""
    if not path:
        got = subprocess.run(["systemctl", "--user", "show-environment"], text=True,
                             capture_output=True)
        for line in got.stdout.splitlines():
            if line.startswith("WAYFIRE_SOCKET="):
                path = line.split("=", 1)[1]
                break
    if not path or not os.path.exists(path):
        raise PrerequisiteMissing(
            "no installed Wayfire IPC session; run this gate on the active PosterChanOS desktop")
    return path


def wayfire(method, path, data=None):
    body = json.dumps({"method": method, "data": data or {}}).encode()
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(10)
        client.connect(path)
        client.sendall(struct.pack("<I", len(body)) + body)
        head = client.recv(4)
        if len(head) != 4:
            raise RuntimeError("Wayfire IPC closed")
        size = struct.unpack("<I", head)[0]
        reply = b""
        while len(reply) < size:
            chunk = client.recv(size - len(reply))
            if not chunk:
                raise RuntimeError("Wayfire IPC closed mid-reply")
            reply += chunk
    result = json.loads(reply)
    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError(result["error"])
    if isinstance(result, dict):
        for key in ("outputs", "views"):
            if isinstance(result.get(key), list):
                return result[key]
    return result


def main():
    owned = subprocess.run(["qfile", "-q", "/opt/posterchan/resources/app.asar"],
                           stdout=subprocess.DEVNULL)
    if owned.returncode:
        raise RuntimeError("installed app.asar is not package-owned")
    path = socket_path()
    result = validate(wayfire("window-rules/list-outputs", path),
                      wayfire("window-rules/list-views", path))
    print("Installed shell output gate passed: " + json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except PrerequisiteMissing as exc:
        print(f"SKIP {exc}", file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        print(f"Installed shell output gate failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
